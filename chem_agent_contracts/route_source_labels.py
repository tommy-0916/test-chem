"""Scoped source-label associations between paper mentions and material entities.

This module resolves which material entity a literal expression in a quoted
excerpt refers to, before any state, quantity, or identity claim about that
mention is evaluated.  It never rewrites the quoted text and never treats two
labels as one when they differ in anything except case and whitespace.

Resolution is deterministic and scoped to one experimental group:

* a controlled-state word embedded in one of the entity's own source labels
  (``solution`` inside the label ``Solution B``) is attributed to that entity;
* a bare controlled-state word (``the solution``) is attributed only to the
  unique entity whose source labels contain that word as a standalone label;
* repeated or foreign mentions are resolved mention by mention; anything that
  still binds to zero entities or to more than one stays unresolved, so
  semantic review keeps it pending instead of a forced unique answer.

A label context is built once per proposal from the graph and its source-bound
facts, then consumed unchanged by the compiler, the literal fact receipt, the
PDF source verifier and the route decision gate.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .route_convention_basis import is_concentration_unit
from .v2 import normalize_material_state

SOURCE_LABEL_RULE_VERSION = "source-labels/v1"
RULE_EMBEDDED_STATE_LABEL = "source-labels/v1:embedded_state_label"
RULE_BARE_STATE_MENTION = "source-labels/v1:bare_state_mention"
RULE_SCOPED_LABEL_IDENTITY = "source-labels/v1:scoped_label_identity"
RULE_DEFINITION_SITE_CONCENTRATION = (
    "source-labels/v1:definition_site_concentration"
)

_PORT_PATH = re.compile(
    r"material_graph\[(0|[1-9][0-9]*)\]\."
    r"(material_inputs|material_intermediates|material_outputs)"
    r"\[(0|[1-9][0-9]*)\]"
)
_PORT_NAME_PATH = re.compile(
    r"material_graph\[(0|[1-9][0-9]*)\]\."
    r"(material_inputs|material_intermediates|material_outputs)"
    r"\[(0|[1-9][0-9]*)\]\.name\Z"
)
# Stage and batch words that carry no identity of their own.  A label built
# only from these words is a referential stage label (``reaction``,
# ``the solution``), not a proper name; removing them leaves the
# distinguishing core that must match across aliases of one entity.
_GENERIC_LABEL_WORDS = frozenset({
    "a", "an", "the", "of", "batch", "mixture", "precursor", "product",
    "reaction", "stage",
})
_PREPARATION_PREDICATE = re.compile(
    r"\s+(?:is|was|are|were)\s+"
    r"(?:prepared|obtained|made|synthesized|produced|formed|created)"
    r"\s+by\s+dissolving\s+",
    re.IGNORECASE,
)
_RECIPE_DISCOURSE = re.compile(
    r"[,;:!?\"“”]|\b(?:no|not|never|neither|without|if|unless|might|may|could|"
    r"would|should|then|before|after|while|when|whereas|but|or|instead|"
    r"rather|is|was|are|were|has|have|had)\b",
    re.IGNORECASE,
)
_RECIPE_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_SOLUTE_AMOUNT = re.compile(
    rf"(?P<value>{_RECIPE_NUMBER})\s*"
    r"(?:kg|mg|ug|ng|g|mmol|umol|nmol|mol)\b\s+(?:of\s+)?(?P<name>.+)\Z",
)
_SOLVENT_AMOUNT = re.compile(
    rf"(?P<value>{_RECIPE_NUMBER})\s*"
    r"(?:mL|uL|nL|L)\b\s+(?:of\s+)?(?P<name>.+)\Z",
)
# An explicit source statement that two mentions are NOT one material.
# Only predicative forms count: "X and Y were two different materials",
# "X and Y are distinct", "X was separated from Y".  Attributive uses such
# as "added to different flasks" do not merge or split anything by
# themselves and are deliberately not matched.
_DISTINCTNESS_CLAIM = re.compile(
    r"\b(?:were|are|was|is)\s+(?:both\s+|two\s+|three\s+)?different\b"
    r"|\bdifferent\s+(?:material|materials|sample|samples|phase|phases|"
    r"product|products|fraction|fractions|batch|batches|entity|entities)\b"
    r"|\b(?:were|are|was|is)\s+distinct\b"
    r"|\bdistinct\s+from\s+each\s+other\b"
    r"|\bseparated\s+from\b",
    re.IGNORECASE,
)


def labels_explicitly_distinct(excerpt: Any, first: str, second: str) -> bool:
    """Whether the passage states these two labels are not one material.

    Both labels must appear as mentions and a predicative distinctness claim
    must cover the pair window.  This is the source-relation backstop for
    identity: proposal-internal consistency (same ID, same instance,
    neighbouring names) can never override an explicit source statement.
    """
    if (not isinstance(excerpt, str) or not excerpt.strip()
            or not isinstance(first, str) or not isinstance(second, str)
            or not first.strip() or not second.strip()):
        return False
    first_pattern = _word_pattern(first.strip(), ignorecase=True)
    second_pattern = _word_pattern(second.strip(), ignorecase=True)
    for first_match in first_pattern.finditer(excerpt):
        for second_match in second_pattern.finditer(excerpt):
            if first_match.start() == second_match.start():
                continue
            start = min(first_match.start(), second_match.start())
            end = max(first_match.end(), second_match.end())
            window = excerpt[start:end + 80]
            if _DISTINCTNESS_CLAIM.search(window):
                return True
    return False


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _entry(record: Any, key: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(key)
    return getattr(record, key, None)


def _entry_text(record: Any, key: str) -> str:
    return _text(_entry(record, key))


def _norm_label(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _word_pattern(value: str, *, ignorecase: bool) -> re.Pattern[str]:
    return re.compile(
        rf"(?<!\w){re.escape(value)}(?!\w)",
        flags=re.IGNORECASE if ignorecase else 0,
    )


@dataclass(frozen=True)
class NameAnchor:
    """One sourced name fact: which label it asserts, quoted where."""

    path: str
    label: str
    excerpt: str


@dataclass(frozen=True)
class MaterialLabelSet:
    """Source-backed labels of one material entity in one experimental group."""

    material_id: str
    labels: frozenset[str]
    surfaces: Mapping[str, frozenset[str]]
    bare_states: frozenset[str]
    name_anchors: tuple[NameAnchor, ...] = ()


@dataclass(frozen=True)
class SourceLabelContext:
    """Per-entity label sets and the unique owner of each label.

    ``label_owner`` covers every label; ``bare_state_owner`` only the labels
    that are controlled state tokens.  A label claimed by two different
    materials attributes nothing to anyone.
    """

    by_material: Mapping[str, MaterialLabelSet] = field(default_factory=dict)
    label_owner: Mapping[str, str] = field(default_factory=dict)
    bare_state_owner: Mapping[str, str] = field(default_factory=dict)


def _port_material(graph: Sequence[Any], field_path: str) -> tuple[str, str]:
    """Return (material_id, sample_id) for a graph port path, or ("", "")."""
    match = _PORT_PATH.match(field_path)
    if match is None or not isinstance(graph, (list, tuple)):
        return "", ""
    step_index = int(match.group(1))
    try:
        step = graph[step_index]
    except (IndexError, TypeError):
        return "", ""
    ports = _entry(step, match.group(2))
    if not isinstance(ports, (list, tuple)):
        return "", ""
    try:
        port = ports[int(match.group(3))]
    except (IndexError, TypeError):
        return "", ""
    return _entry_text(port, "material_id"), _entry_text(step, "sample_id")


def _fact_excerpt(fact: Any) -> str:
    return (
        _entry_text(fact, "verification_excerpt")
        or _entry_text(fact, "excerpt")
        or _entry_text(_entry(fact, "provenance"), "excerpt")
    )


def _same_quote(left: str, right: str) -> bool:
    """Whether two excerpts quote the same passage (one may extend the other)."""
    a = re.sub(r"\s+", " ", left).strip()
    b = re.sub(r"\s+", " ", right).strip()
    return bool(a and b and (a in b or b in a))


def build_source_label_context(
    graph: Sequence[Any],
    facts: Sequence[Any] = (),
) -> SourceLabelContext:
    """Bind each entity's labels to their literal surface forms in its facts.

    Labels come from source-bound ``.name`` facts of the entity's ports.
    Surface forms are the literal spellings of those labels found in the
    entity's own fact excerpts: proposal spellings plus case variants such as
    a sentence-initial ``Solution B``.  Two labels never merge here; a
    surface belongs to exactly one normalized label of exactly one entity.
    Every label keeps its name facts as anchors, so a mention can be tied
    back to the exact quoted passage where the proposal asserts the entity
    carries that label.
    """
    labels_by_entity: dict[str, set[str]] = {}
    excerpts_by_entity: dict[str, list[str]] = {}
    anchors_by_entity: dict[str, list[NameAnchor]] = {}
    for fact in facts:
        path = _entry_text(fact, "field_path")
        if not path:
            continue
        material_id, _sample_id = _port_material(graph, path)
        if not material_id:
            continue
        excerpt = _fact_excerpt(fact)
        if excerpt:
            excerpts_by_entity.setdefault(material_id, []).append(excerpt)
        if _PORT_NAME_PATH.fullmatch(path):
            label = _norm_label(_entry_text(fact, "value"))
            if label:
                labels_by_entity.setdefault(material_id, set()).add(label)
                anchors_by_entity.setdefault(material_id, []).append(NameAnchor(
                    path=path,
                    label=label,
                    excerpt=excerpt,
                ))

    by_material: dict[str, MaterialLabelSet] = {}
    label_owner: dict[str, str] = {}
    bare_owner: dict[str, str] = {}
    for material_id, labels in labels_by_entity.items():
        surfaces: dict[str, set[str]] = {label: set() for label in labels}
        for label in labels:
            pattern = _word_pattern(label, ignorecase=True)
            for excerpt in excerpts_by_entity.get(material_id, []):
                for match in pattern.finditer(excerpt):
                    surface = match.group()
                    if _norm_label(surface) == label:
                        surfaces[label].add(surface)
        bare_states = frozenset(
            label for label in labels
            if normalize_material_state(label) != "unknown"
        )
        by_material[material_id] = MaterialLabelSet(
            material_id=material_id,
            labels=frozenset(labels),
            surfaces={label: frozenset(found) for label, found in surfaces.items()},
            bare_states=bare_states,
            name_anchors=tuple(anchors_by_entity.get(material_id, ())),
        )
        for label in labels:
            if label in label_owner and label_owner[label] != material_id:
                # The same label naming two different materials attributes
                # nothing to anyone; drop it into ambiguity.
                label_owner[label] = ""
            else:
                label_owner[label] = material_id
        for token in bare_states:
            if token in bare_owner and bare_owner[token] != material_id:
                bare_owner[token] = ""
            else:
                bare_owner[token] = material_id
    return SourceLabelContext(
        by_material=by_material,
        label_owner={
            label: owner for label, owner in label_owner.items() if owner
        },
        bare_state_owner={
            token: owner for token, owner in bare_owner.items() if owner
        },
    )


def _anchor_path(entity: MaterialLabelSet, label: str, excerpt: str) -> str:
    """The name fact that anchors this label at this quoted passage, if any."""
    for anchor in entity.name_anchors:
        if anchor.label == label and _same_quote(anchor.excerpt, excerpt):
            return anchor.path
    return ""


def attributed_state_mention(
    source_value: Any,
    excerpt: Any,
    field_path: str,
    graph: Sequence[Any],
    context: SourceLabelContext,
) -> dict[str, str] | None:
    """Resolve one state-word mention of this port's entity, or stay unresolved.

    Every occurrence of the state word is classified by the longest label
    surface that contains it: a span of this entity binds the mention through
    that label; a span of any other entity makes it foreign; with no span at
    all, a bare controlled-state token may bind to its unique owner, but only
    when a name fact of this entity quotes the same passage — uniqueness in
    the proposal's label table alone is not evidence of what the source says.
    The claim passes only when exactly one mention binds to this port's
    entity; zero or several remain ``semantic_binding_pending``.
    """
    source = _text(source_value)
    if not source or not isinstance(excerpt, str) or not excerpt.strip():
        return None
    material_id, _sample_id = _port_material(graph, field_path)
    entity = context.by_material.get(material_id)
    if entity is None:
        return None
    token = _norm_label(source)

    def contradicted(label: str) -> bool:
        # An explicit source statement that this label and another label of
        # the same entity are not one material overrides every internal
        # consistency signal (same ID, same instance, anchored quote).
        return any(
            other != label and labels_explicitly_distinct(excerpt, label, other)
            for other in entity.labels
        )

    spans: list[tuple[int, int, str, str, str]] = []
    for owner_id, candidate in context.by_material.items():
        for label, label_surfaces in candidate.surfaces.items():
            for surface in label_surfaces:
                for match in _word_pattern(
                        surface, ignorecase=False).finditer(excerpt):
                    spans.append(
                        (match.start(), match.end(), owner_id, label, surface),
                    )
    bindings: list[dict[str, str]] = []
    for match in _word_pattern(source, ignorecase=True).finditer(excerpt):
        start, end = match.start(), match.end()
        containing = [
            span for span in spans if span[0] <= start and end <= span[1]
        ]
        if containing:
            best = max(containing, key=lambda span: (
                span[1] - span[0], span[3] != token,
            ))
            if best[2] != material_id:
                continue  # a foreign entity's own label mention
            label = best[3]
            is_bare = label == token and token in entity.bare_states
            if context.label_owner.get(label) != material_id:
                # Two materials claim this label: the mention is ambiguous
                # for everyone until review decides the name facts.
                continue
            anchor = _anchor_path(entity, label, excerpt)
            if is_bare and not anchor:
                # A bare label mention attributes only where a name fact of
                # this entity quotes this same passage; the label table
                # alone is not evidence of what the source says here.
                continue
            if contradicted(label):
                continue
            rule = (RULE_BARE_STATE_MENTION if is_bare
                    else RULE_EMBEDDED_STATE_LABEL)
            bindings.append({
                "schema_version": "source-label-binding/v1",
                "rule_version": SOURCE_LABEL_RULE_VERSION,
                "rule_id": rule,
                "label": label,
                "source_surface": best[4],
                "entity_material_id": material_id,
                "mention_anchor": anchor,
            })
            continue
        owner = context.bare_state_owner.get(token)
        if token not in entity.bare_states or owner != material_id:
            continue
        anchor = _anchor_path(entity, token, excerpt)
        if not anchor:
            # The label table may name this entity alone, yet this passage
            # never asserts the label for it: nothing source-grounded binds.
            continue
        if contradicted(token):
            continue
        bindings.append({
            "schema_version": "source-label-binding/v1",
            "rule_version": SOURCE_LABEL_RULE_VERSION,
            "rule_id": RULE_BARE_STATE_MENTION,
            "label": token,
            "source_surface": match.group(),
            "entity_material_id": material_id,
            "mention_anchor": anchor,
        })
    if len(bindings) != 1:
        return None
    return bindings[0]


def state_attribution_outcome(
    source_value: Any,
    excerpt: Any,
    field_path: str,
    graph: Sequence[Any],
    context: SourceLabelContext,
) -> tuple[str, dict[str, str] | None]:
    """Three-way state attribution gate, uniform across every consumer.

    Returns ``("binding", record)`` when exactly one mention binds to the
    port's entity.  ``("legacy", None)`` means the association layer has no
    opinion for this claim: either the proposal carries no name facts for
    this entity at all, or no entity label and no bare-state candidacy for
    this word appears in the excerpt — only then may the old strictly-local
    matcher still apply.  ``("pending", None)`` means the association layer
    engaged (label spans or bare-state candidacy exist for this word) but
    resolution failed; a failed association check never falls back to the
    legacy matcher.
    """
    source = _text(source_value)
    material_id, _sample_id = _port_material(graph, field_path)
    entity = context.by_material.get(material_id)
    if entity is None:
        return "legacy", None
    binding = attributed_state_mention(
        source_value, excerpt, field_path, graph, context,
    )
    if binding is not None:
        return "binding", binding
    if not source or not isinstance(excerpt, str) or not excerpt.strip():
        return "pending", None
    token = _norm_label(source)
    if any(token in other.bare_states for other in context.by_material.values()):
        return "pending", None
    mentions = list(_word_pattern(source, ignorecase=True).finditer(excerpt))
    for other in context.by_material.values():
        for label_surfaces in other.surfaces.values():
            for surface in label_surfaces:
                for span in _word_pattern(
                        surface, ignorecase=False).finditer(excerpt):
                    if any(span.start() <= m.start() and m.end() <= span.end()
                           for m in mentions):
                        # The word sits inside a label span (own or foreign):
                        # the association engaged and failed to bind it.
                        return "pending", None
    return "legacy", None


def quantity_identity_surfaces(
    field_path: str,
    graph: Sequence[Any],
    context: SourceLabelContext,
) -> tuple[str, ...]:
    """Scoped label surfaces for a material amount path.

    Surfaces are the literal spellings of the port entity's labels, including
    verified case variants found in that entity's own fact excerpts.  They
    never cross experimental groups and never include unit or formula tokens,
    so ``Solution B`` and ``solution B`` match while ``Solution A`` stays a
    different entity and ``M`` or ``NaOH`` never enters the label set.
    """
    material_id, _sample_id = _port_material(graph, field_path)
    entity = context.by_material.get(material_id)
    if entity is None:
        return ()
    found: list[str] = []
    for label in sorted(entity.labels):
        for surface in sorted(entity.surfaces.get(label, frozenset())):
            if surface not in found:
                found.append(surface)
    return tuple(found)


def competing_quantity_identity_surfaces(
    field_path: str,
    graph: Sequence[Any],
    context: SourceLabelContext,
) -> tuple[str, ...]:
    """Source surfaces of other entities, never aliases of this port's entity."""
    material_id, _sample_id = _port_material(graph, field_path)
    return tuple(sorted({
        surface
        for owner_id, entity in context.by_material.items()
        if owner_id != material_id
        for surfaces in entity.surfaces.values()
        for surface in surfaces
    }))


def _quantified_dissolution_operands(recipe: str) -> tuple[str, ...] | None:
    """Recognize one quantified dissolution, rather than arbitrary prose.

    This bounded source grammar accepts mass/amount-of-substance solutes in
    a quantified solvent volume.  It cannot certify mixing, dilution,
    multiple events or unquantified stock inputs; those remain pending.
    Parentheses inside literal chemical names are retained, but may not
    enclose another concentration or a subordinate operation.
    """
    if _RECIPE_DISCOURSE.search(recipe):
        return None
    parts = re.split(r"\s+in\s+", recipe, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    solutes = re.split(r"\s+and\s+", parts[0], flags=re.IGNORECASE)
    operands: list[str] = []
    for text, pattern in [
        *((item, _SOLUTE_AMOUNT) for item in solutes),
        (parts[1], _SOLVENT_AMOUNT),
    ]:
        match = pattern.fullmatch(text.strip())
        if match is None:
            return None
        value = float(match.group("value"))
        if not math.isfinite(value) or value <= 0:
            return None
        name = match.group("name").strip()
        # A secondary event or a stock concentration can never be certified
        # by the dissolution definition.  Ingredient lists are permitted;
        # scientific prose following an ingredient is not parsed here.
        if re.search(
                r"\b(?:and|or|by|followed|subsequently)\b|"
                r"\d\s*(?:M|mM|mol/L|mmol/L)\b", name):
            return None
        if any(normalize_material_state(word) in {"solution", "suspension"}
               or word.casefold() in {"stock", "buffer"}
               for word in re.findall(r"\w+", name)):
            return None
        depth = 0
        for char in name:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    return None
        if depth:
            return None
        operands.append(name)
    return tuple(operands)


def _single_solute_aqueous_dissolution(recipe: Any) -> bool:
    """Check whether the recipe is one quantified reagent dissolved in water.

    In a preparation sentence, the trailing parenthetical describes the
    prepared solution (the grammatical subject).  A mention of the single
    quantified reagent or of the water inside such a recipe is an ingredient
    role, not a competing concentration bearer: a reagent's amount is already
    stated in its own quantity, and pure water carries no solute
    concentration.  This is a grammatical-role judgment over the parsed
    operands only — never an arithmetic n/V(solvent) inference, which would
    be unsound because concentration is defined against the solution volume.
    Multi-solute recipes and non-water solvents stay competing, since the
    parenthetical may then describe one component or the solvent itself.
    """
    if not isinstance(recipe, str):
        return False
    operands = _quantified_dissolution_operands(recipe)
    if operands is None or len(operands) != 2:
        return False
    solvent = operands[-1].strip().casefold()
    return solvent.removeprefix("of ").strip() == "water"


def definition_site_concentration_binding(
    excerpt: Any,
    value: Any,
    unit: Any,
    surfaces: Sequence[str],
    *,
    competing_surfaces: Sequence[str] = (),
) -> dict[str, str] | None:
    """Bind a trailing parenthesized concentration to its definition sentence.

    The entity's literal label must be the sole subject immediately followed
    by an affirmative preparation predicate and one quantified dissolution
    recipe that parses as a single-reagent aqueous dissolution — this
    applicability is judged from the parsed recipe itself, never from which
    competing entities a caller listed.  Outside that scope (multi-solute
    recipes, non-aqueous solvents) the parenthetical may describe a
    component or the solvent itself, so those passages are always
    unresolved.  Inside the scope, a final parenthesized concentration
    describes the prepared solution (the subject): a competing source-bound
    entity mentioned inside the recipe is an ingredient role (reagent or
    water), not a competing concentration bearer, and does not block the
    binding.  Any other co-occurring source-bound entity — another solution,
    a carrier or stock, or an entity mentioned outside the recipe — can
    itself bear the reported concentration and stays unresolved.  Stock
    inputs, coordination, negation and multiple events also stay unresolved.
    This fallback is intentionally a bounded source grammar, not a
    natural-language concentration resolver and never an arithmetic
    concentration proof.
    """
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not isinstance(excerpt, str) or not excerpt.strip()
            or not isinstance(unit, str) or not unit.strip()
            or not is_concentration_unit(unit.strip()) or not surfaces):
        return None
    normalized = excerpt.replace("−", "-").replace("–", "-").replace("µ", "u")
    unit_pattern = r"\s*".join(
        re.escape(piece) for piece in re.split(r"\s+", unit.strip().replace("µ", "u"))
    )
    amount_pattern = re.compile(
        rf"(?<![\w.])([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
        rf"\s*{unit_pattern}(?![\w/])",
        re.IGNORECASE,
    )
    occurrences = [
        match for match in amount_pattern.finditer(normalized)
        if math.isclose(
            float(match.group(1)), float(value), rel_tol=1e-12, abs_tol=1e-12,
        )
    ]
    if len(occurrences) != 1:
        return None
    amount = occurrences[0]
    remainder = normalized[amount.end():]
    if not re.fullmatch(r"\s*\)\s*\.?\s*", remainder):
        return None  # the concentration must close a trailing parenthesis
    if not normalized[:amount.start()].rstrip().endswith("("):
        return None
    boundaries = list(re.finditer(r"[.!?](?=\s|$)", normalized[:amount.start()]))
    sentence_start = boundaries[-1].end() if boundaries else 0
    sentence = normalized[sentence_start:].strip()
    surface_pattern = re.compile(
        "|".join(
            rf"(?<!\w){re.escape(surface)}(?!\w)"
            for surface in sorted(set(surfaces), key=len, reverse=True)
        )
    )
    mentions = list(surface_pattern.finditer(sentence))
    if len(mentions) != 1:
        return None
    subject = mentions[0]
    if sentence[:subject.start()].strip().casefold() not in {"", "the"}:
        return None
    predicate = _PREPARATION_PREDICATE.match(sentence, subject.end())
    if predicate is None:
        return None
    concentration_start = sentence.rfind("(")
    if concentration_start < predicate.end():
        return None
    recipe = sentence[predicate.end():concentration_start].strip()
    operands = _quantified_dissolution_operands(recipe)
    if operands is None:
        return None
    if not _single_solute_aqueous_dissolution(recipe):
        # Applicability gate, checked against the parsed recipe itself —
        # never against which competing entities a caller happened to list.
        # Multi-solute recipes and non-aqueous solvents can put the
        # parenthetical on a component or on the solvent itself, so the
        # definition-site rule does not certify them at all.
        return None
    recipe_start = predicate.end()
    recipe_end = concentration_start
    for surface in competing_surfaces:
        if not isinstance(surface, str) or not surface.strip():
            continue
        pattern = _word_pattern(surface, ignorecase=False)
        for mention in pattern.finditer(sentence):
            if (subject.start() <= mention.start()
                    and mention.end() <= subject.end()):
                if mention.end() - mention.start() < subject.end() - subject.start():
                    continue  # a shorter foreign stage word inside this subject label
                # A competing surface spanning the whole subject means the
                # entities cannot be told apart at all.
                return None
            if recipe_start <= mention.start() and mention.end() <= recipe_end:
                # Ingredient role (the quantified reagent or the water) in
                # the applicable single-solute aqueous dissolution: not a
                # competing concentration bearer.
                continue
            return None
    surface = subject.group()
    return {
        "schema_version": "source-label-binding/v1",
        "rule_version": SOURCE_LABEL_RULE_VERSION,
        "rule_id": RULE_DEFINITION_SITE_CONCENTRATION,
        "label": _norm_label(surface),
        "source_surface": surface,
        "entity_material_id": "",
        "mention_anchor": "",
    }


def surface_anchor(
    field_path: str,
    graph: Sequence[Any],
    context: SourceLabelContext,
    surface: str,
    excerpt: Any,
) -> str:
    """The name fact (field path) that anchors this surface at this passage."""
    material_id, _sample_id = _port_material(graph, field_path)
    entity = context.by_material.get(material_id)
    if entity is None or not isinstance(excerpt, str):
        return ""
    return _anchor_path(entity, _norm_label(surface), excerpt)


__all__ = [
    "RULE_BARE_STATE_MENTION",
    "RULE_DEFINITION_SITE_CONCENTRATION",
    "RULE_EMBEDDED_STATE_LABEL",
    "RULE_SCOPED_LABEL_IDENTITY",
    "SOURCE_LABEL_RULE_VERSION",
    "NameAnchor",
    "SourceLabelContext",
    "attributed_state_mention",
    "build_source_label_context",
    "competing_quantity_identity_surfaces",
    "definition_site_concentration_binding",
    "labels_explicitly_distinct",
    "quantity_identity_surfaces",
    "state_attribution_outcome",
    "surface_anchor",
]
