"""Typed state-proof DAG with recomputable source leaves.

The flat convention engine (``route_convention_basis``) proves ONE output or
input state at a time and gates every parent state on literal checks.  This
module composes those single-hop proofs into a content-addressed DAG
(``state-proof-dag/v1``): every node carries a typed schema, a claim, and
premises referencing other nodes by id, and every source fact becomes a
``source-evidence-leaf/v1`` that can be re-located inside the signed group
blocks from its binding locator and projected character span alone.

Node and leaf ids are content addresses: ``sha256`` of the canonical JSON
payload (keys sorted, ``","``/``":"`` separators, ``ensure_ascii=False``)
with the id field itself excluded.  Premises are canonicalized by sorting on
``role`` before hashing.  Any change to a node's content — including a
premise id — changes its own id, so a tampered node either fails id
recomputation or leaves a dangling premise reference.

The engine is reused, never reimplemented: convention nodes lift their typed
fields from a freshly recomputed flat proof, with a
``_VerifiedParentStateEvidence`` capability token minted STRUCTURALLY —
``_mint_verified_parent_token(host, node)`` requires the minting host (a
``_DagBuilder`` or ``StateProofDagVerifier``) to own the premise node (a
builder: registered in its own node table with identical content; a
verifier: part of the DAG currently under verification and already
verified clean) and extracts the triple FROM THE NODE's claim — only
after the premise claim's binding triple (field path, material instance
id, target state) has been checked against the dependent node's typed
fields — the exact parent-state proposition the flat proof references
(``proof_dag_parent_state_binding_mismatch`` otherwise).  No bare-string
verified-mint API exists anywhere.  Every internal derive call site
additionally guards the token's exact type
(``_assert_verified_parent_token`` / ``_assert_verified_liquid_token``),
so a diagnostic what-if assumption — accepted by the flat engine as the
labeled what-if channel — can never enter the proof layer's consumption
path.  The parent premise of a convention node is the canonical chain
node for THIS step's own input state path (a paper literal when literal
gates prove it, an inheritance node otherwise), never the upstream
step's output node directly.

A second, narrowly-scoped proof class composes through the optional
``liquid_medium`` premise role: a ``protocol_reference`` node
(``protocol-reference-proof/v1``) certifies that a step's affirmed operation
references a protocol defined earlier in the SAME experimental group and
inherits exactly the definition's ``operation_sequence`` and
``liquid_medium`` — never execution counts, retained objects, output states,
or material identities.  The dependent convention node binds the premise to
its own operation (path, value and operation evidence id,
``proof_dag_liquid_medium_binding_mismatch`` otherwise) and the flat
recompute receives a ``_VerifiedLiquidMedium`` capability token that
discharges only the liquid-participation gate for that exact operation
binding.  The ``retained_object`` role stays exclusively bound to
``source_relation`` nodes
(``proof_dag_retained_object_binding_mismatch`` otherwise).

Verified minting is bound to a HOST-HELD, verified snapshot that cannot
move with caller-side edits (Round 3D safety closure 3), in three layers:
(1) no shared mutable references — ``_DagBuilder.build()`` returns a deep
copy of the node table so the builder's registry is never aliased to a
caller-held DAG, and ``StateProofDagVerifier.verify()`` deep-copies the
incoming node table into ``self._active_nodes`` at install time and
records ``self._active_digests`` (per-node recomputed content addresses)
from the original passed nodes; (2) a minting window — the verifier's
minting context lives only while ``verify()`` executes and is cleared in
a ``finally`` on exit, and the builder mints only while
``build()``/``build_node()`` executes (a depth counter set at entry,
cleared in a ``finally``), so any post-window mint attempt raises; (3)
extraction from the snapshot, never from the caller's object — the
structural mint uses the passed node only to identify the node by
``node_id`` and to integrity-check it (its recomputed content address
must equal the recorded snapshot digest), additionally requiring every
node in the passed node's transitive premise closure to be present in the
snapshot and digest-equal on the caller side where that side is reachable
(the verifier retains the caller's table as ``_active_source`` for the
window duration only), and the minted triple is extracted from the
host-held snapshot copy.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import re
from typing import Any, Callable, Mapping, Sequence

from . import route_convention_basis as _basis
from .route_convention_basis import (
    _INPUT_STATE, _INHERITANCE_RULE_ID, _OUTPUT_STATE, _RULE_EVENTS,
    _evidence_id, _inheritance_proof_for_evidence, _items, _literal_in_quote,
    _mapping, _resolve_parent_output, _text, convention_fact_evidence_by_id,
    derive_unreviewed_output_state,
)
from .route_field_basis import (
    controlled_state_mapping, state_source_locally_attributed,
)
from .route_retained_object import (
    POST_OPERATION_RETAINED_OBJECT_RULE_ID,
    POST_OPERATION_RETAINED_OBJECT_RULE_VERSION,
    derive_post_operation_retained_object,
)
from .route_protocol_reference import (
    PROTOCOL_REFERENCE_PROOF_SCHEMA,
    PROTOCOL_REFERENCE_RULE_ID,
    PROTOCOL_REFERENCE_RULE_VERSION,
    resolve_protocol_reference,
)

SOURCE_EVIDENCE_LEAF_SCHEMA = "source-evidence-leaf/v1"
PAPER_LITERAL_PROOF_SCHEMA = "paper-literal-proof/v1"
SOURCE_RELATION_PROOF_SCHEMA = "source-relation-proof/v1"
SAME_STATE_CONVENTION_PROOF_SCHEMA = "same-state-convention-proof/v1"
STATE_CHANGE_CONVENTION_PROOF_SCHEMA = "state-change-convention-proof/v1"
STATE_INHERITANCE_PROOF_SCHEMA = "state-inheritance-proof/v1"
STATE_PROOF_DAG_SCHEMA = "state-proof-dag/v1"

_NODE_TYPE_SCHEMAS = {
    "paper_literal": PAPER_LITERAL_PROOF_SCHEMA,
    "source_relation": SOURCE_RELATION_PROOF_SCHEMA,
    "same_state": SAME_STATE_CONVENTION_PROOF_SCHEMA,
    "state_change": STATE_CHANGE_CONVENTION_PROOF_SCHEMA,
    "inheritance": STATE_INHERITANCE_PROOF_SCHEMA,
    "protocol_reference": PROTOCOL_REFERENCE_PROOF_SCHEMA,
}
_CONVENTION_ROLES = frozenset({
    "parent_state", "operation", "retained_object", "liquid_medium",
})
_SOURCE_RELATION_ROLES = ["naming", "operation", "output_name"]
_PROTOCOL_REFERENCE_ROLES = ["definition_evidence", "reference_evidence"]
_OPERATION_PATH = re.compile(r"material_graph\[(0|[1-9][0-9]*)\]\.operation\Z")
# Binding fields of a route fact: the identity a source leaf stands on.
_FACT_BINDING_FIELDS = ("fact_id", "field_path", "value", "unit", "excerpt")


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _digest_text(text: str) -> str:
    return "sha256_" + sha256(text.encode("utf-8")).hexdigest()


def _normalize_excerpt(text: str) -> str:
    """The projection normalization used by the span resolver."""
    try:
        from reaserch_agent.route_pdf_quote_binding import (
            normalize_pdf_quote_whitespace,
        )
        return normalize_pdf_quote_whitespace(text)
    except Exception:
        return " ".join(text.split())


def _fact_digest(fact: Mapping[str, Any]) -> str:
    """Digest of the fact's binding fields (identity, path, value, quote)."""
    binding = {field: fact.get(field) for field in _FACT_BINDING_FIELDS}
    return _digest_text(_canonical(binding))


def leaf_id_for(leaf: Mapping[str, Any]) -> str:
    """Recompute a source-evidence leaf's content address."""
    payload = {key: value for key, value in leaf.items() if key != "leaf_id"}
    return "source_leaf_" + sha256(
        _canonical(payload).encode("utf-8")
    ).hexdigest()[:24]


def node_id_for(node: Mapping[str, Any]) -> str:
    """Recompute a proof node's content address."""
    payload = {key: value for key, value in node.items() if key != "node_id"}
    return "proof_node_" + sha256(
        _canonical(payload).encode("utf-8")
    ).hexdigest()[:24]


def _sorted_premises(premises: Sequence[Mapping[str, str]]) -> list[dict]:
    """Premises are canonicalized by sorting on role before hashing."""
    return [
        {"role": str(premise["role"]), "node_id": str(premise["node_id"])}
        for premise in sorted(premises, key=lambda item: str(item["role"]))
    ]


def _claim(
    field_path: str, target_state: str = "",
    material_id: str = "", material_instance_id: str = "",
) -> dict:
    return {
        "field_path": field_path,
        "target_state": target_state,
        "material_id": material_id,
        "material_instance_id": material_instance_id,
    }


def _role_premise(node: Mapping[str, Any], role: str) -> Mapping[str, str]:
    """The unique premise carrying ``role`` (roles are deduped upstream)."""
    return next(
        premise for premise in node["premises"] if premise["role"] == role
    )


def _parent_state_binding_issue(
    premise_node: Mapping[str, Any], *, field_path: str,
    material_instance_id: str, target_state: str,
) -> str:
    """The parent-state premise binding check, shared by builder and verifier.

    A node hanging on role ``parent_state`` proves nothing unless the
    premise node's claim is EXACTLY the parent-state proposition the
    dependent node's own (recomputed) proof references: same field path,
    same material instance id, same target state.  An otherwise-valid but
    unrelated node substituted into the role — or the correct ancestor at
    the wrong chain level — fails here, even though every node verifies on
    its own and every content address is honestly recomputed.
    """
    claim = _mapping(premise_node.get("claim"))
    if (_text(claim.get("field_path")) != field_path
            or _text(claim.get("material_instance_id")) != material_instance_id
            or _text(claim.get("target_state")) != target_state):
        return "proof_dag_parent_state_binding_mismatch"
    return ""


def _mint_closure(snapshot: Mapping[str, Any], node_id: str) -> list[str]:
    """The transitive premise closure of ``node_id`` inside a snapshot.

    Every premise id referenced anywhere upstream of the node, the node's
    own id first.  A premise id missing from the snapshot raises: the
    mint must never stand on a dependency the host has not verified.
    Cycles cannot occur in a verified DAG; ``seen`` keeps the walk total
    regardless.
    """
    closure: list[str] = []
    seen: set[str] = set()
    stack = [node_id]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        node = snapshot.get(current)
        if node is None:
            raise ValueError(
                "verified mint refused: a premise in the node's transitive "
                "closure is not part of the host's verified node snapshot"
            )
        seen.add(current)
        closure.append(current)
        premises = node.get("premises")
        if isinstance(premises, list):
            for premise in premises:
                if (isinstance(premise, Mapping)
                        and isinstance(premise.get("node_id"), str)):
                    stack.append(premise["node_id"])
    return closure


def _structural_mint_binding(host: Any, node: Any) -> Mapping[str, Any]:
    """Bind a verified mint to its host's verified node SNAPSHOT.

    Verified capability tokens may be minted ONLY by the proof-DAG layer's
    two hosts, only inside the host's minting window, and only for a node
    whose verified snapshot record cannot have moved with caller-side
    edits (Round 3D safety closure 3):

    - Window: a ``_DagBuilder`` mints only while ``build()`` /
      ``build_node()`` is executing (``_mint_depth > 0``); a
      ``StateProofDagVerifier`` mints only while ``verify()`` is running
      (its ``_active_nodes`` / ``_active_memo`` / ``_active_digests`` /
      ``_active_source`` context is installed at verification start and
      cleared in a ``finally`` on exit).  Any mint attempt outside the
      window raises ``ValueError``.
    - Identification + integrity: the passed node is used ONLY to
      identify the node by its ``node_id`` field and to integrity-check
      it — its recomputed content address must equal the digest recorded
      for that id at verification time (the verifier's
      ``_active_digests``; for the builder the registry key itself, which
      IS the honestly recomputed content address).
    - Dependency closure: every node in the passed node's transitive
      premise closure must be present in the host's snapshot, the
      snapshot copy's own recomputed digest must match the recorded one,
      and — where the caller's side of the table is reachable (the
      verifier retains it as ``_active_source`` for the window duration
      only) — the caller-side ancestor's recomputed digest must equal the
      snapshot digest.  An old verification record therefore never
      authorizes new content anywhere upstream of the node (the
      "target unchanged, dependency mutated" case).
    - Memo: on the verifier, the target node's memo entry must be ``""``
      (verified clean in this run; premises verify before the nodes
      standing on them, so this holds for every premise at mint time
      inside ``verify()``).
    - Extraction: the returned node — the object the minted triple is
      extracted from — is the HOST-HELD SNAPSHOT copy (the verifier's
      ``_active_nodes`` deep copy, the builder's registry), never the
      passed object.

    Any other host raises ``TypeError``; a closed window, an
    unregistered, substituted, mutated, or not-yet-clean node, and any
    dependency-closure mismatch raise ``ValueError``.  This is what makes
    the mint structural: there is no way to mint a verified token for a
    proposition the DAG layer has not itself proven AS VERIFIED.
    """
    if not isinstance(node, Mapping) or not isinstance(node.get("node_id"), str):
        raise ValueError(
            "verified mint requires a proof node mapping with a node_id"
        )
    node_id = node["node_id"]
    if isinstance(host, _DagBuilder):
        if host._mint_depth < 1:
            raise ValueError(
                "verified mint refused: the builder's minting window is "
                "closed — minting is valid only while build()/build_node() "
                "is executing"
            )
        snapshot: Mapping[str, Any] = host.nodes
        digests: Mapping[str, str] | None = None  # keys ARE the digests
        caller_table: Mapping[str, Any] | None = None
        memo: dict[str, str] | None = None
    elif isinstance(host, StateProofDagVerifier):
        if (host._active_nodes is None or host._active_memo is None
                or host._active_digests is None):
            raise ValueError(
                "verified mint refused: no verification is in progress — "
                "the minting window is open only while verify() executes"
            )
        snapshot = host._active_nodes
        digests = host._active_digests
        caller_table = host._active_source
        memo = host._active_memo
    else:
        raise TypeError(
            "verified mint refused: the host must be a _DagBuilder or a "
            "StateProofDagVerifier"
        )
    if node_id not in snapshot:
        raise ValueError(
            "verified mint refused: the node is not part of the host's "
            "verified node snapshot"
        )
    expected = digests[node_id] if digests is not None else node_id
    if node_id_for(node) != expected:
        raise ValueError(
            "verified mint refused: the passed node's content does not "
            "match the verified snapshot digest recorded for its node_id"
        )
    if memo is not None and memo.get(node_id) != "":
        raise ValueError(
            "verified mint refused: the node has not verified clean "
            "in the current verification"
        )
    for ancestor_id in _mint_closure(snapshot, node_id):
        ancestor_expected = (
            digests[ancestor_id] if digests is not None else ancestor_id
        )
        if node_id_for(snapshot[ancestor_id]) != ancestor_expected:
            raise ValueError(
                "verified mint refused: the host's verified node snapshot "
                "no longer matches its recorded digests inside the node's "
                "dependency closure"
            )
        if caller_table is not None:
            caller_ancestor = caller_table.get(ancestor_id)
            if (caller_ancestor is None
                    or node_id_for(caller_ancestor) != ancestor_expected):
                raise ValueError(
                    "verified mint refused: a dependency in the node's "
                    "transitive premise closure changed since verification"
                )
    return snapshot[node_id]


def _mint_verified_parent_token(host: Any, node: Any) -> Any:
    """Mint the parent-state capability token FROM a verified premise node.

    The binding triple (``claim.field_path`` / ``claim.target_state`` /
    ``claim.material_instance_id``) is extracted from the node itself —
    the helper takes no caller-supplied value strings.  Minted only after
    the host-ownership check passed (and, at the call sites, after
    ``_parent_state_binding_issue`` passed for the node); the engine
    re-checks the triple against its own computed parent binding and
    fails closed on any mismatch, so the token never widens what the
    premise proves.
    """
    node = _structural_mint_binding(host, node)
    claim = _mapping(node.get("claim"))
    return _basis._VerifiedParentStateEvidence(
        _text(claim.get("field_path")),
        _text(claim.get("target_state")),
        _text(claim.get("material_instance_id")),
        _key=_basis._MINT_KEY,
    )


def _assert_verified_parent_token(token: Any) -> Any:
    """Exact-type guard at the proof layer's internal derive boundary.

    The structural mint only ever produces ``_VerifiedParentStateEvidence``,
    so this guard fires only on tampering — e.g. a diagnostic what-if
    assumption (accepted by the flat engine as the labeled assumption
    channel) smuggled into the proof-DAG consumption path.  ``None``
    passes: it is the legacy literal-gate default, not a token.
    """
    if token is not None and type(token) is not _basis._VerifiedParentStateEvidence:
        raise TypeError(
            "proof-DAG derive boundary requires exactly "
            "_VerifiedParentStateEvidence (diagnostic assumptions are "
            "never proof artifacts)"
        )
    return token


def _liquid_medium_binding_issue(
    premise_node: Mapping[str, Any], *, operation_path: str,
    operation_evidence_id: str, operation_value: str,
) -> str:
    """The liquid-medium premise binding check, shared by builder and verifier.

    A node hanging on role ``liquid_medium`` certifies this step's liquid
    participation only when it is a ``protocol_reference`` node whose
    reference evidence is exactly this step's operation evidence, whose
    operation binding (claim field path + recorded operation value) equals
    this step's operation, and whose inherited medium is non-empty.  An
    otherwise-valid but unrelated node substituted into the role fails here
    even though every content address is honestly recomputed.
    """
    claim = _mapping(premise_node.get("claim"))
    if (premise_node.get("node_type") != "protocol_reference"
            or _text(premise_node.get("reference_evidence_id"))
            != operation_evidence_id
            or _text(claim.get("field_path")) != operation_path
            or _text(premise_node.get("operation_value")) != operation_value
            or not _text(premise_node.get("liquid_medium"))):
        return "proof_dag_liquid_medium_binding_mismatch"
    return ""


def _mint_verified_liquid_token(host: Any, node: Any) -> Any:
    """Mint the liquid-medium capability token FROM a verified premise.

    The certifying triple (``operation_value`` / ``liquid_medium`` /
    ``definition_digest``) is extracted from the ``protocol_reference``
    node itself — the helper takes no caller-supplied value strings.
    Minted only after the host-ownership check passed (and, at the call
    sites, after ``_liquid_medium_binding_issue`` passed for the node);
    the engine re-checks the triple against its own computed operation
    binding and fails closed on any mismatch, so the token never widens
    what the premise proves — and it discharges nothing except the
    liquid-participation gate.
    """
    node = _structural_mint_binding(host, node)
    return _basis._VerifiedLiquidMedium(
        _text(node.get("operation_value")),
        _text(node.get("liquid_medium")),
        _text(node.get("definition_digest")),
        _key=_basis._MINT_KEY,
    )


def _assert_verified_liquid_token(token: Any) -> Any:
    """Exact-type guard at the proof layer's internal derive boundary.

    The structural mint only ever produces ``_VerifiedLiquidMedium``, so
    this guard fires only on tampering — e.g. a diagnostic what-if
    assumption (accepted by the flat engine as the labeled assumption
    channel) smuggled into the proof-DAG consumption path.  ``None``
    passes: it is the legacy literal-gate default, not a token.
    """
    if token is not None and type(token) is not _basis._VerifiedLiquidMedium:
        raise TypeError(
            "proof-DAG derive boundary requires exactly "
            "_VerifiedLiquidMedium (diagnostic assumptions are never "
            "proof artifacts)"
        )
    return token


def _protocol_reference_candidates(
    facts: Sequence[Any], *, paper_id: str, experimental_group_id: str,
    source_digest: str, span_of: Any,
) -> list[dict]:
    """Every locatable fact excerpt as a raw protocol-resolution candidate.

    Resolution filters these to true definition mentions inside the pinned
    group scope; a fact whose excerpt does not locate in the signed blocks
    cannot be a definition mention and is dropped here.
    """
    candidates: list[dict] = []
    locate = getattr(span_of, "locate", None)
    for fact in facts:
        excerpt = _text(_mapping(fact).get("excerpt"))
        fact_id = _text(_mapping(fact).get("fact_id"))
        if not excerpt or not fact_id or not callable(locate):
            continue
        located = locate(excerpt)
        if located is None:
            continue
        candidates.append({
            "excerpt": excerpt,
            "evidence_id": _evidence_id(paper_id, experimental_group_id, fact_id),
            "field_path": _text(_mapping(fact).get("field_path")),
            "locator": located[1],
            "char_span": [located[2][0], located[2][1]],
            "paper_id": paper_id,
            "experimental_group_id": experimental_group_id,
            "source_digest": source_digest,
        })
    return candidates


def _source_evidence_leaf(
    fact: Mapping[str, Any], *, paper_id: str, experimental_group_id: str,
    source_digest: str, section: str, span_of: Any,
) -> dict:
    """Project one route fact into a recomputable source-evidence leaf."""
    excerpt = _text(fact.get("excerpt"))
    locator = ""
    char_span: list[int] = []
    if span_of is not None and excerpt:
        locate = getattr(span_of, "locate", None)
        located = locate(excerpt) if callable(locate) else None
        if located is not None:
            locator = located[1]
            if located[2][0] >= 0:
                char_span = [located[2][0], located[2][1]]
    leaf = {
        "schema_version": SOURCE_EVIDENCE_LEAF_SCHEMA,
        "paper_id": paper_id,
        "experimental_group_id": experimental_group_id,
        "source_digest": source_digest,
        "section": section,
        "locator": locator,
        "char_span": char_span,
        "excerpt_digest": _digest_text(_normalize_excerpt(excerpt)),
        "field_path": _text(fact.get("field_path")),
        "claim_value": fact.get("value"),
        "unit": _text(fact.get("unit")),
        "fact_digest": _fact_digest(fact),
    }
    leaf["leaf_id"] = leaf_id_for(leaf)
    return leaf


def _literal_state_gates(
    field_path: str, value: str, excerpt: str, port: Mapping[str, Any],
) -> dict | None:
    """The flat literal parent gates, evaluated for a paper-literal node."""
    _state_mapping, issue = controlled_state_mapping(
        field_path, value, port.get("state"),
    )
    if issue:
        return None
    if not state_source_locally_attributed(value, excerpt, port.get("name")):
        return None
    return {"controlled_state_mapping": True, "local_attribution": True}


def _port_at(graph: Sequence[Any], field_path: str) -> Mapping[str, Any] | None:
    """Resolve a state/name port path inside the typed graph."""
    match = _OUTPUT_STATE.fullmatch(field_path)
    if match is not None:
        step_index, port_index = int(match.group(1)), int(match.group(2))
        if step_index >= len(graph):
            return None
        ports = _items(_mapping(graph[step_index]).get("material_outputs"))
        return ports[port_index] if port_index < len(ports) else None
    match = _INPUT_STATE.fullmatch(field_path)
    if match is not None:
        step_index, collection, port_index = (
            int(match.group(1)), match.group(2), int(match.group(3)),
        )
        if step_index >= len(graph):
            return None
        ports = _items(_mapping(graph[step_index]).get(collection))
        return ports[port_index] if port_index < len(ports) else None
    return None


def _upstream_matches(
    graph: Sequence[Any], reference: Mapping[str, Any],
) -> list[int]:
    """Every step index whose output port matches one parent-output ref."""
    ref_step = _text(reference.get("macro_step_id"))
    ref_instance = _text(reference.get("material_instance_id"))
    return [
        index for index, earlier in enumerate(graph)
        for port in _items(_mapping(earlier).get("material_outputs"))
        if (_text(_mapping(earlier).get("macro_step_id")) == ref_step
            and _text(port.get("material_instance_id")) == ref_instance)
    ]


class _DagBuilder:
    """Recursive builder with a per-build memo and a cycle guard."""

    def __init__(
        self, graph: Sequence[Any], facts: Sequence[Any], *, paper_id: str,
        experimental_group_id: str, source_digest: str, section: str = "",
        span_of: Any = None,
    ) -> None:
        self.graph = (
            [_mapping(step) for step in graph]
            if isinstance(graph, (list, tuple)) else []
        )
        self.facts = (
            [_mapping(fact) for fact in facts]
            if isinstance(facts, (list, tuple)) else []
        )
        self.paper_id = paper_id
        self.experimental_group_id = experimental_group_id
        self.source_digest = source_digest
        self.section = section
        self.span_of = span_of
        self.by_path: dict[str, Mapping[str, Any]] = {}
        for fact in self.facts:
            path = _text(fact.get("field_path"))
            if path and path not in self.by_path:
                self.by_path[path] = fact
        self.by_fact_id = {
            _text(fact.get("fact_id")): fact
            for fact in self.facts if _text(fact.get("fact_id"))
        }
        self.evidence = convention_fact_evidence_by_id(
            self.facts, paper_id=paper_id,
            experimental_group_id=experimental_group_id,
        )
        self.nodes: dict[str, dict] = {}
        self.memo: dict[str, str] = {}
        self.visiting: set[str] = set()
        self.resolver = self._resolver_from_span()
        # Minting window depth: > 0 only while build()/build_node() is
        # executing.  The structural mint refuses any attempt outside
        # that window.
        self._mint_depth = 0

    def _resolver_from_span(self) -> Callable[[str], tuple[Any, str]] | None:
        """A retained-object resolver recomputing records live from span_of."""
        if self.span_of is None:
            return None
        records: dict[str, Any] = {}
        issues: dict[str, str] = {}
        for step_index, step in enumerate(self.graph):
            record, issue = derive_post_operation_retained_object(
                self.graph, self.facts, step_index, span_of=self.span_of,
            )
            outputs = _items(step.get("material_outputs"))
            fallback = (
                f"material_graph[{step_index}].material_outputs[0].state"
                if outputs else ""
            )
            if record is not None:
                records[_text(_mapping(record.get("output")).get("state_path"))] = record
            elif fallback:
                issues[fallback] = issue

        def resolve(field_path: str) -> tuple[Any, str]:
            record = records.get(field_path)
            if record is not None:
                return record, ""
            return None, issues.get(field_path, "")

        return resolve

    def build(self, field_path: str) -> tuple[dict | None, str]:
        self._mint_depth += 1
        try:
            node_id, issue = self.build_node(field_path)
            if node_id is None:
                return None, issue
            _rules, resource_digest = _basis._rule_resource()
            dag = {
                "schema_version": STATE_PROOF_DAG_SCHEMA,
                # A deep copy: the caller-held DAG must never alias the
                # builder's registry, so mutating the returned DAG cannot
                # move what a later in-window mint would extract.
                "nodes": deepcopy(self.nodes),
                "root_id": node_id,
                "context": {
                    "graph_digest": _digest_text(_canonical(self.graph)),
                    "paper_id": self.paper_id,
                    "experimental_group_id": self.experimental_group_id,
                    "source_digest": self.source_digest,
                    "rule_resource_digest": resource_digest,
                },
            }
            return dag, ""
        finally:
            self._mint_depth -= 1

    def build_node(self, field_path: str) -> tuple[str | None, str]:
        self._mint_depth += 1
        try:
            if field_path in self.memo:
                return self.memo[field_path], ""
            if field_path in self.visiting:
                return None, "proof_dag_cycle"
            self.visiting.add(field_path)
            try:
                if _OUTPUT_STATE.fullmatch(field_path) is not None:
                    node_id, issue = self._build_output_state(field_path)
                elif _INPUT_STATE.fullmatch(field_path) is not None:
                    node_id, issue = self._build_input_state(field_path)
                elif (_OPERATION_PATH.fullmatch(field_path) is not None
                      or field_path.endswith(".name")):
                    node_id, issue = self._build_non_state_fact(field_path)
                else:
                    return None, "semantic_binding_pending"
                if node_id is not None:
                    self.memo[field_path] = node_id
                return node_id, issue
            finally:
                self.visiting.discard(field_path)
        finally:
            self._mint_depth -= 1

    # -- node constructors -------------------------------------------------

    def _add_node(self, node: dict) -> str:
        node["node_id"] = node_id_for(node)
        self.nodes[node["node_id"]] = node
        return node["node_id"]

    def _literal_node(
        self, fact: Mapping[str, Any], *, claim: Mapping[str, Any],
        gates: Mapping[str, Any],
    ) -> str:
        node = {
            "schema_version": PAPER_LITERAL_PROOF_SCHEMA,
            "node_type": "paper_literal",
            "claim": dict(claim),
            "premises": [],
            "rule_id": "",
            "rule_version": "",
            "rule_resource_digest": "",
            "leaf": _source_evidence_leaf(
                fact, paper_id=self.paper_id,
                experimental_group_id=self.experimental_group_id,
                source_digest=self.source_digest, section=self.section,
                span_of=self.span_of,
            ),
            "gates": dict(gates),
        }
        return self._add_node(node)

    def _literal_state_node(
        self, field_path: str, fact: Mapping[str, Any], port: Mapping[str, Any],
        gates: Mapping[str, Any],
    ) -> str:
        return self._literal_node(
            fact,
            claim=_claim(
                field_path, _text(port.get("state")),
                _text(port.get("material_id")),
                _text(port.get("material_instance_id")),
            ),
            gates=gates,
        )

    def _build_non_state_fact(self, field_path: str) -> tuple[str | None, str]:
        """Operation and name facts stand alone as paper-literal leaves."""
        fact = self.by_path.get(field_path)
        if fact is None:
            return None, "convention_fact_path_missing_or_duplicate"
        value = _text(fact.get("value"))
        excerpt = _text(fact.get("excerpt"))
        if _OPERATION_PATH.fullmatch(field_path) is not None:
            if not _literal_in_quote(value, excerpt):
                return None, "convention_operation_fact_unbound"
        elif value.casefold() not in excerpt.casefold():
            return None, "convention_operation_fact_unbound"
        node_id = self._literal_node(
            fact, claim=_claim(field_path),
            gates={"value_literal_in_quote": True},
        )
        return node_id, ""

    def _operation_leaf(self, step_index: int) -> tuple[str | None, str]:
        operation_path = f"material_graph[{step_index}].operation"
        fact = self.by_path.get(operation_path)
        if fact is None:
            return None, "convention_operation_fact_missing"
        if not _literal_in_quote(
            _text(fact.get("value")), _text(fact.get("excerpt")),
        ):
            return None, "convention_operation_fact_unbound"
        return self._literal_node(
            fact, claim=_claim(operation_path),
            gates={"value_literal_in_quote": True},
        ), ""

    def _source_relation_node(
        self, record: Mapping[str, Any],
    ) -> tuple[str | None, str]:
        """The post-operation retained-object relation on its three leaves."""
        output = _mapping(record.get("output"))
        leaves: dict[str, str] = {}
        for role, fact_id in (
            ("operation", _text(record.get("operation_fact_id"))),
            ("naming", _text(record.get("naming_fact_id"))),
            ("output_name", _text(record.get("output_name_fact_id"))),
        ):
            fact = self.by_fact_id.get(fact_id)
            if fact is None:
                return None, "retained_object_output_binding_unresolved"
            leaves[role] = self._literal_node(
                fact, claim=_claim(_text(fact.get("field_path"))),
                gates={"value_literal_in_quote": True},
            )
        node = {
            "schema_version": SOURCE_RELATION_PROOF_SCHEMA,
            "node_type": "source_relation",
            "subtype": "post_operation_retained_object",
            "claim": _claim(
                _text(output.get("state_path")),
                _text((_port_at(self.graph, _text(output.get("state_path")))
                       or {}).get("state")),
                _text(output.get("material_id")),
                _text(output.get("material_instance_id")),
            ),
            "premises": _sorted_premises(
                {"role": role, "node_id": leaves[role]}
                for role in _SOURCE_RELATION_ROLES
            ),
            "rule_id": POST_OPERATION_RETAINED_OBJECT_RULE_ID,
            "rule_version": POST_OPERATION_RETAINED_OBJECT_RULE_VERSION,
            "rule_resource_digest": "",
            "record": json.loads(json.dumps(record)),
        }
        return self._add_node(node), ""

    # -- state paths ---------------------------------------------------------

    def _build_protocol_reference(
        self, step_index: int,
    ) -> tuple[str | None, str]:
        """Build the protocol_reference node certifying a step's liquid medium.

        The reference phrase is the step's own operation fact (value +
        quote); resolution runs against every locatable fact excerpt in the
        pinned group scope via ``route_protocol_reference``.  The
        definition premise is the paper literal of the resolved definition
        fact; the reference premise is the step's operation paper literal
        (content-addressed, so it dedupes with the convention node's
        operation premise).
        """
        if self.span_of is None:
            return None, "proof_dag_resolver_missing"
        operation_path = f"material_graph[{step_index}].operation"
        reference_fact = self.by_path.get(operation_path)
        if reference_fact is None:
            return None, "convention_operation_fact_missing"
        reference_evidence_id = _evidence_id(
            self.paper_id, self.experimental_group_id,
            _text(reference_fact.get("fact_id")),
        )
        locate = getattr(self.span_of, "locate", None)
        located = (
            locate(_text(reference_fact.get("excerpt")))
            if callable(locate) else None
        )
        if located is None:
            return None, "protocol_reference_position_unknown"
        record, issue = resolve_protocol_reference(
            _text(reference_fact.get("value")),
            _text(reference_fact.get("excerpt")),
            reference_evidence_id=reference_evidence_id,
            reference_locator=located[1],
            reference_position=located[2][0],
            candidates=_protocol_reference_candidates(
                self.facts, paper_id=self.paper_id,
                experimental_group_id=self.experimental_group_id,
                source_digest=self.source_digest, span_of=self.span_of,
            ),
            paper_id=self.paper_id,
            experimental_group_id=self.experimental_group_id,
            source_digest=self.source_digest,
        )
        if record is None:
            return None, issue
        definition_fact = self.evidence.get(
            _text(record.get("definition_evidence_id")))
        if definition_fact is None:
            return None, "proof_dag_protocol_reference_mismatch"
        value = _text(definition_fact.get("value"))
        excerpt = _text(definition_fact.get("excerpt"))
        definition_path = _text(definition_fact.get("field_path"))
        if _OPERATION_PATH.fullmatch(definition_path) is not None:
            bound = _literal_in_quote(value, excerpt)
        else:
            bound = bool(value) and value.casefold() in excerpt.casefold()
        if not bound:
            return None, "convention_operation_fact_unbound"
        definition_node_id = self._literal_node(
            definition_fact,
            claim=_claim(definition_path),
            gates={"value_literal_in_quote": True},
        )
        reference_node_id, issue = self._operation_leaf(step_index)
        if reference_node_id is None:
            return None, issue
        node = {
            "schema_version": PROTOCOL_REFERENCE_PROOF_SCHEMA,
            "node_type": "protocol_reference",
            "claim": _claim(operation_path),
            "premises": _sorted_premises([
                {"role": "definition_evidence", "node_id": definition_node_id},
                {"role": "reference_evidence", "node_id": reference_node_id},
            ]),
            "rule_id": PROTOCOL_REFERENCE_RULE_ID,
            "rule_version": PROTOCOL_REFERENCE_RULE_VERSION,
            "rule_resource_digest": "",
            "operation_path": operation_path,
            "operation_value": _text(
                _mapping(self.graph[step_index]).get("operation")),
            "protocol_name": _text(record.get("protocol_name")),
            "operation_sequence": json.loads(json.dumps(
                record.get("operation_sequence") or [])),
            "liquid_medium": _text(record.get("liquid_medium")),
            "definition_evidence_id": _text(
                record.get("definition_evidence_id")),
            "reference_evidence_id": _text(record.get("reference_evidence_id")),
            "definition_ordinal_anchor": _text(
                record.get("definition_ordinal_anchor")),
            "reference_ordinal": _text(record.get("reference_ordinal")),
            "definition_execution_count": _text(
                record.get("definition_execution_count")),
            "reference_execution_count": _text(
                record.get("reference_execution_count")),
            "definition_digest": _text(record.get("definition_digest")),
            "paper_id": self.paper_id,
            "experimental_group_id": self.experimental_group_id,
            "source_digest": self.source_digest,
        }
        return self._add_node(node), ""

    # -- state paths ---------------------------------------------------------

    def _build_output_state(self, field_path: str) -> tuple[str | None, str]:
        match = _OUTPUT_STATE.fullmatch(field_path)
        step_index, output_index = int(match.group(1)), int(match.group(2))
        if step_index >= len(self.graph):
            return None, "convention_graph_path_missing"
        step = self.graph[step_index]
        outputs, inputs = (
            _items(step.get("material_outputs")),
            _items(step.get("material_inputs")),
        )
        if output_index >= len(outputs):
            return None, "convention_graph_path_missing"
        child = outputs[output_index]
        child_id = _text(child.get("material_instance_id"))
        relations = [
            relation for relation in _items(step.get("material_relations"))
            if child_id in relation.get("output_material_instance_ids", [])
        ]
        if len(relations) != 1:
            return None, "convention_material_relation_missing_or_ambiguous"
        relation = relations[0]
        parent_ids = relation.get("input_material_instance_ids")
        if not isinstance(parent_ids, list) or len(parent_ids) != 1:
            return None, "convention_parent_relation_ambiguous"
        parents = [
            (index, port) for index, port in enumerate(inputs)
            if _text(port.get("material_instance_id")) == _text(parent_ids[0])
        ]
        if len(parents) != 1:
            return None, "convention_parent_material_missing"
        input_index, parent_port = parents[0]
        parent_state_path = (
            f"material_graph[{step_index}]"
            f".material_inputs[{input_index}].state"
        )
        parent_fact = self.by_path.get(parent_state_path)

        operation_node_id, issue = self._operation_leaf(step_index)
        if operation_node_id is None:
            return None, issue

        record = None
        if self.resolver is not None:
            record, _record_issue = self.resolver(field_path)

        # Parent premise: literal gates first, then the canonical chain
        # through THIS step's own input state path — an inheritance node (or
        # a paper literal when the input state is literal-provable), never
        # the upstream step's output node directly.
        parent_node_id: str | None = None
        verified_parent: Any = None
        if parent_fact is not None:
            gates = _literal_state_gates(
                parent_state_path, _text(parent_fact.get("value")),
                _text(parent_fact.get("excerpt")), parent_port,
            )
            if gates is not None:
                parent_node_id = self._literal_state_node(
                    parent_state_path, parent_fact, parent_port, gates,
                )
        if (parent_node_id is None
                and _text(parent_port.get("material_origin")) == "upstream_output"):
            references = parent_port.get("parent_output_refs")
            if isinstance(references, list) and len(references) == 1:
                ref_step, ref_output, ref_port = _resolve_parent_output(
                    self.graph, step_index, _mapping(references[0]),
                )
                if ref_port is not None:
                    parent_node_id, issue = self.build_node(parent_state_path)
                    if parent_node_id is None:
                        return None, issue
                    # The token is minted only after the premise claim's
                    # binding triple is checked against this step's own
                    # computed parent binding, and only from the node
                    # registered in this builder (structural mint).
                    binding_issue = _parent_state_binding_issue(
                        self.nodes[parent_node_id],
                        field_path=parent_state_path,
                        material_instance_id=_text(parent_ids[0]),
                        target_state=_text(parent_port.get("state")),
                    )
                    if binding_issue:
                        return None, binding_issue
                    verified_parent = _mint_verified_parent_token(
                        self, self.nodes[parent_node_id])
        if parent_node_id is None:
            # The engine's honest issue for this exact configuration.
            _proof, honest = derive_unreviewed_output_state(
                self.graph, self.facts, field_path,
                paper_id=self.paper_id,
                experimental_group_id=self.experimental_group_id,
                source_digest=self.source_digest,
                retained_object_resolver=self.resolver,
            )
            return None, honest or "convention_parent_state_unverified"

        proof, issue = derive_unreviewed_output_state(
            self.graph, self.facts, field_path,
            paper_id=self.paper_id,
            experimental_group_id=self.experimental_group_id,
            source_digest=self.source_digest,
            retained_object_resolver=self.resolver,
            verified_parent_state=_assert_verified_parent_token(
                verified_parent),
        )
        medium_node_id: str | None = None
        if proof is None and issue == "convention_liquid_participation_missing":
            # The step's liquid medium may be inherited from a protocol
            # definition resolved inside the pinned group scope: build the
            # protocol_reference node, bind it to THIS step's operation
            # exactly, and re-derive with the capability token.  Any
            # failure keeps the engine's honest liquid-participation issue.
            medium_node_id, _medium_issue = self._build_protocol_reference(
                step_index)
            if medium_node_id is not None:
                operation_fact = self.by_path.get(
                    f"material_graph[{step_index}].operation")
                binding_issue = _liquid_medium_binding_issue(
                    self.nodes[medium_node_id],
                    operation_path=f"material_graph[{step_index}].operation",
                    operation_evidence_id=_evidence_id(
                        self.paper_id, self.experimental_group_id,
                        _text(_mapping(operation_fact).get("fact_id"))),
                    operation_value=_text(step.get("operation")),
                )
                if binding_issue:
                    return None, binding_issue
                proof, issue = derive_unreviewed_output_state(
                    self.graph, self.facts, field_path,
                    paper_id=self.paper_id,
                    experimental_group_id=self.experimental_group_id,
                    source_digest=self.source_digest,
                    retained_object_resolver=self.resolver,
                    verified_parent_state=_assert_verified_parent_token(
                        verified_parent),
                    verified_liquid_medium=_assert_verified_liquid_token(
                        _mint_verified_liquid_token(
                            self, self.nodes[medium_node_id])),
                )
                if proof is None:
                    medium_node_id = None
        if proof is None:
            return None, issue

        premises = [
            {"role": "operation", "node_id": operation_node_id},
            {"role": "parent_state", "node_id": parent_node_id},
        ]
        if medium_node_id is not None:
            premises.append({"role": "liquid_medium", "node_id": medium_node_id})
        retained_object_fields = {
            key: proof[key] for key in proof if key.startswith("retained_object")
        }
        if retained_object_fields:
            if record is None:
                return None, "retained_object_output_binding_unresolved"
            relation_node_id, issue = self._source_relation_node(record)
            if relation_node_id is None:
                return None, issue
            premises.append({"role": "retained_object", "node_id": relation_node_id})
        rule_id = _text(proof.get("rule_id"))
        node_type = "same_state" if rule_id in _RULE_EVENTS else "state_change"
        node = {
            "schema_version": _NODE_TYPE_SCHEMAS[node_type],
            "node_type": node_type,
            "claim": _claim(
                field_path, _text(proof.get("target_state")),
                _text(child.get("material_id")), child_id,
            ),
            "premises": _sorted_premises(premises),
            "rule_id": rule_id,
            "rule_version": _text(proof.get("rule_version")),
            "rule_resource_digest": _text(proof.get("resource_digest")),
            "relation_id": _text(proof.get("relation_id")),
            "segment_id": _text(relation.get("source_operation_ref")),
            "parent_instance_id": _text(proof.get("parent_instance_id")),
            "child_instance_id": _text(proof.get("child_instance_id")),
            "parent_state_path": _text(proof.get("parent_state_path")),
            "parent_source_value": _text(proof.get("parent_source_value")),
            "parent_evidence_id": _text(proof.get("parent_evidence_id")),
            "operation_path": _text(proof.get("operation_path")),
            "operation_evidence_id": _text(proof.get("operation_evidence_id")),
        }
        if retained_object_fields:
            node["retained_object_fields"] = retained_object_fields
        return self._add_node(node), ""

    def _build_input_state(self, field_path: str) -> tuple[str | None, str]:
        match = _INPUT_STATE.fullmatch(field_path)
        step_index, collection, port_index = (
            int(match.group(1)), match.group(2), int(match.group(3)),
        )
        if step_index >= len(self.graph):
            return None, "convention_graph_path_missing"
        step = self.graph[step_index]
        ports = _items(step.get(collection))
        if port_index >= len(ports):
            return None, "convention_graph_path_missing"
        port = ports[port_index]
        fact = self.by_path.get(field_path)
        if fact is None:
            return None, "semantic_binding_pending"
        if _text(fact.get("value")) != _text(port.get("state")):
            return None, "convention_child_state_value_mismatch"
        gates = _literal_state_gates(
            field_path, _text(fact.get("value")),
            _text(fact.get("excerpt")), port,
        )
        if gates is not None:
            return self._literal_state_node(field_path, fact, port, gates), ""
        if _text(port.get("material_origin")) != "upstream_output":
            return None, "convention_parent_state_unverified"
        references = port.get("parent_output_refs")
        if not isinstance(references, list) or len(references) != 1:
            return None, "convention_upstream_reference_missing"
        reference = _mapping(references[0])
        matches = _upstream_matches(self.graph, reference)
        if matches and all(index >= step_index for index in matches):
            # An inheritance edge may only reach backwards in the graph.
            return None, "proof_dag_future_reference"
        ref_step, ref_output, ref_port = _resolve_parent_output(
            self.graph, step_index, reference,
        )
        if ref_port is None:
            return None, "convention_upstream_reference_mismatch"
        parent_output_path = (
            f"material_graph[{ref_step}]"
            f".material_outputs[{ref_output}].state"
        )
        parent_node_id, issue = self.build_node(parent_output_path)
        if parent_node_id is None:
            return None, issue
        parent_node = self.nodes[parent_node_id]
        if parent_node.get("node_type") not in ("state_change", "same_state"):
            return None, "convention_parent_state_unverified"
        # The premise claim must be exactly the resolved upstream output
        # state this input port carries forward.
        binding_issue = _parent_state_binding_issue(
            parent_node,
            field_path=parent_output_path,
            material_instance_id=_text(reference.get("material_instance_id")),
            target_state=_text(ref_port.get("state")),
        )
        if binding_issue:
            return None, binding_issue
        # The capability token certifies the GRANDPARENT binding — the
        # parent output node's own parent_state premise claim — so the
        # re-derived parent proof needs no literal gate for a non-literal
        # grandparent state already proven in this DAG (arbitrary chains of
        # non-literal hops compose through this recursion).  Minted
        # structurally from the premise node registered in this builder.
        verified_grandparent = _mint_verified_parent_token(
            self,
            self.nodes[_role_premise(parent_node, "parent_state")["node_id"]])
        grandparent_fact = self.evidence.get(
            _text(parent_node.get("parent_evidence_id")))
        operation_fact = self.evidence.get(
            _text(parent_node.get("operation_evidence_id")))
        record, issue = _inheritance_proof_for_evidence(
            self.graph, field_path,
            parent_source_value=_text(parent_node.get("parent_source_value")),
            parent_excerpt=(_text(grandparent_fact.get("excerpt"))
                            if grandparent_fact is not None else ""),
            parent_evidence_id=_text(parent_node.get("parent_evidence_id")),
            operation_excerpt=(_text(operation_fact.get("excerpt"))
                               if operation_fact is not None else ""),
            operation_evidence_id=_text(parent_node.get("operation_evidence_id")),
            paper_id=self.paper_id,
            experimental_group_id=self.experimental_group_id,
            source_digest=self.source_digest,
            retained_object_resolver=self.resolver,
            facts=self.facts,
            verified_parent_state=_assert_verified_parent_token(
                verified_grandparent),
        )
        if record is None:
            return None, issue
        node = {
            "schema_version": STATE_INHERITANCE_PROOF_SCHEMA,
            "node_type": "inheritance",
            "claim": _claim(
                field_path, _text(record.get("target_state")),
                _text(port.get("material_id")),
                _text(record.get("child_instance_id")),
            ),
            "premises": _sorted_premises(
                [{"role": "parent_state", "node_id": parent_node_id}],
            ),
            "rule_id": _text(record.get("rule_id")),
            "rule_version": _text(record.get("rule_version")),
            "rule_resource_digest": "",
            "parent_ref": {
                "kind": "material_instance",
                "macro_step_id": _text(reference.get("macro_step_id")),
                "material_instance_id": _text(reference.get("material_instance_id")),
            },
            "parent_instance_id": _text(record.get("parent_instance_id")),
            "child_instance_id": _text(record.get("child_instance_id")),
            "parent_state_path": _text(record.get("parent_state_path")),
            "parent_source_value": _text(record.get("parent_source_value")),
            "parent_evidence_id": _text(record.get("parent_evidence_id")),
            "operation_path": _text(record.get("operation_path")),
            "operation_evidence_id": _text(record.get("operation_evidence_id")),
            "relation_id": _text(record.get("relation_id")),
        }
        return self._add_node(node), ""


def build_state_proof_dag(
    graph: Sequence[Any], facts: Sequence[Any], field_path: str, *,
    paper_id: str, experimental_group_id: str, source_digest: str,
    section: str = "", span_of: Any = None,
) -> tuple[dict | None, str]:
    """Build the typed proof DAG rooted at one state field path."""
    builder = _DagBuilder(
        graph, facts, paper_id=paper_id,
        experimental_group_id=experimental_group_id,
        source_digest=source_digest, section=section, span_of=span_of,
    )
    return builder.build(field_path)


class StateProofDagVerifier:
    """Recompute a whole proof DAG against one pinned context.

    The context — graph digest, source scope and the live rule-resource
    digest — is fixed at construction; a DAG built under any other context
    fails with ``proof_dag_context_mismatch``.  Verification is structural
    first (premise existence, acyclicity, depth), then content: every node id
    is recomputed from its payload, and every node type is re-derived from
    the signed blocks, the facts and the versioned rules.  Any failure
    invalidates the whole DAG; there is no partial pass.
    """

    def __init__(
        self, graph: Sequence[Any], facts: Sequence[Any], *, paper_id: str,
        experimental_group_id: str, source_digest: str, span_of: Any = None,
        blocks: Sequence[Any] = None, caption_block_locators: Sequence[str] = (),
    ) -> None:
        self.graph = (
            [_mapping(step) for step in graph]
            if isinstance(graph, (list, tuple)) else []
        )
        self.facts = (
            [_mapping(fact) for fact in facts]
            if isinstance(facts, (list, tuple)) else []
        )
        self.paper_id = paper_id
        self.experimental_group_id = experimental_group_id
        self.source_digest = source_digest
        if span_of is None and blocks:
            from .route_retained_object import build_excerpt_span_resolver

            span_of = build_excerpt_span_resolver(blocks, caption_block_locators)
        self.span_of = span_of
        self.blocks = (
            [(locator, text) for locator, text in blocks] if blocks else None
        )
        self.captions = tuple(caption_block_locators or ())
        self.by_path: dict[str, Mapping[str, Any]] = {}
        for fact in self.facts:
            path = _text(fact.get("field_path"))
            if path and path not in self.by_path:
                self.by_path[path] = fact
        self.by_fact_id = {
            _text(fact.get("fact_id")): fact
            for fact in self.facts if _text(fact.get("fact_id"))
        }
        self.evidence = convention_fact_evidence_by_id(
            self.facts, paper_id=paper_id,
            experimental_group_id=experimental_group_id,
        )
        self.resolver = self._resolver_from_span()
        self._projection: str | None = None
        # Verification context binding the structural mints, installed
        # ONLY while verify() runs and cleared in a finally on exit —
        # the minting window:
        # ``_active_nodes`` is a DEEP COPY of the node table under
        # verification (caller-side edits after install cannot move it),
        # ``_active_digests`` records each node's content address
        # recomputed from the ORIGINAL passed nodes at install time,
        # ``_active_memo`` is the per-node issue memo ("" means verified
        # clean), and ``_active_source`` retains the caller's node table
        # by reference for the window duration ONLY, so the structural
        # mint can re-check the caller side's transitive premise closure
        # against the recorded digests.  A verified capability token can
        # be minted only in-window, for a node whose memo entry is "",
        # whose passed content still matches the recorded digest, and
        # whose dependency closure is intact on both sides.
        self._active_nodes: Mapping[str, Any] | None = None
        self._active_memo: dict[str, str] | None = None
        self._active_digests: Mapping[str, str] | None = None
        self._active_source: Mapping[str, Any] | None = None
        _rules, resource_digest = _basis._rule_resource()
        self.context = {
            "graph_digest": _digest_text(_canonical(self.graph)),
            "paper_id": paper_id,
            "experimental_group_id": experimental_group_id,
            "source_digest": source_digest,
            "rule_resource_digest": resource_digest,
        }

    def _resolver_from_span(self) -> Callable[[str], tuple[Any, str]] | None:
        if self.span_of is None:
            return None
        records: dict[str, Any] = {}
        issues: dict[str, str] = {}
        for step_index, step in enumerate(self.graph):
            record, issue = derive_post_operation_retained_object(
                self.graph, self.facts, step_index, span_of=self.span_of,
            )
            outputs = _items(step.get("material_outputs"))
            fallback = (
                f"material_graph[{step_index}].material_outputs[0].state"
                if outputs else ""
            )
            if record is not None:
                records[_text(_mapping(record.get("output")).get("state_path"))] = record
            elif fallback:
                issues[fallback] = issue

        def resolve(field_path: str) -> tuple[Any, str]:
            record = records.get(field_path)
            if record is not None:
                return record, ""
            return None, issues.get(field_path, "")

        return resolve

    # -- top level -----------------------------------------------------------

    def _clear_mint_context(self) -> None:
        self._active_nodes = None
        self._active_memo = None
        self._active_digests = None
        self._active_source = None

    def verify(self, dag: Any) -> str:
        # A new verification supersedes any previous minting context.
        self._clear_mint_context()
        if not isinstance(dag, Mapping) or (
            dag.get("schema_version") != STATE_PROOF_DAG_SCHEMA
        ):
            return "proof_dag_invalid"
        nodes = dag.get("nodes")
        root_id = dag.get("root_id")
        if (not isinstance(nodes, Mapping) or not nodes
                or any(not isinstance(node, Mapping)
                       for node in nodes.values())
                or not isinstance(root_id, str)):
            return "proof_dag_invalid"
        if dag.get("context") != self.context:
            return "proof_dag_context_mismatch"
        if root_id not in nodes:
            return "proof_dag_root_missing"
        # Structural pass: premise existence, acyclicity, depth.  This runs
        # before any content check so a dangling premise id and a mutual
        # premise cycle are named exactly.
        for node in nodes.values():
            premises = node.get("premises")
            if not isinstance(premises, list):
                return "proof_dag_invalid"
            for premise in premises:
                if (not isinstance(premise, Mapping)
                        or not isinstance(premise.get("role"), str)
                        or not isinstance(premise.get("node_id"), str)):
                    return "proof_dag_invalid"
                if premise["node_id"] not in nodes:
                    return "proof_dag_premise_missing"
        issue = self._structure_check(nodes)
        if issue:
            return issue
        memo: dict[str, str] = {}
        # Install the minting context FOR THE DURATION of the content
        # pass: premises verify before the nodes standing on them, so
        # when a convention/inheritance node mints a capability token for
        # a premise mid-verification, the premise's memo entry is already
        # "" (verified clean).  The node table is deep-copied into the
        # snapshot and per-node digests are recorded from the ORIGINAL
        # passed nodes now, so caller-side edits after this point can
        # neither move the snapshot nor re-use the recorded digests; the
        # caller's table is retained by reference for the window only so
        # the mint can re-check the caller side's dependency closure.
        # The finally revokes the whole context: no mint is possible once
        # verify() has returned.
        self._active_nodes = deepcopy(dict(nodes))
        self._active_digests = {
            node_id: node_id_for(node) for node_id, node in nodes.items()
        }
        self._active_memo = memo
        self._active_source = nodes
        try:
            for node_id in nodes:
                issue = self._verify_node(node_id, nodes, memo, set(), 1)
                if issue:
                    return issue
            return ""
        finally:
            self._clear_mint_context()

    def _structure_check(self, nodes: Mapping[str, Any]) -> str:
        color: dict[str, int] = {}

        def visit(node_id: str, depth: int) -> str:
            state = color.get(node_id, 0)
            if state == 1:
                return "proof_dag_cycle"
            if state == 2:
                return ""
            if depth > len(nodes):
                return "proof_dag_depth_exceeded"
            color[node_id] = 1
            for premise in nodes[node_id]["premises"]:
                issue = visit(premise["node_id"], depth + 1)
                if issue:
                    return issue
            color[node_id] = 2
            return ""

        for node_id in nodes:
            issue = visit(node_id, 1)
            if issue:
                return issue
        return ""

    def _verify_node(
        self, node_id: str, nodes: Mapping[str, Any], memo: dict[str, str],
        visiting: set[str], depth: int,
    ) -> str:
        if node_id in memo:
            return memo[node_id]
        if node_id in visiting:
            return "proof_dag_cycle"
        if depth > len(nodes):
            return "proof_dag_depth_exceeded"
        visiting.add(node_id)
        node = nodes[node_id]
        issue = self._verify_node_content(node, nodes, memo, visiting, depth)
        visiting.discard(node_id)
        memo[node_id] = issue
        return issue

    def _verify_node_content(
        self, node: Mapping[str, Any], nodes: Mapping[str, Any],
        memo: dict[str, str], visiting: set[str], depth: int,
    ) -> str:
        if node_id_for(node) != node.get("node_id"):
            return "proof_dag_node_id_mismatch"
        node_type = node.get("node_type")
        if _NODE_TYPE_SCHEMAS.get(node_type) != node.get("schema_version"):
            return "proof_dag_node_mismatch"
        roles = [premise["role"] for premise in node["premises"]]
        if roles != sorted(roles) or len(set(roles)) != len(roles):
            return "proof_dag_premise_roles_mismatch"
        role_set = set(roles)
        expected = {
            "paper_literal": set(),
            "source_relation": set(_SOURCE_RELATION_ROLES),
            "inheritance": {"parent_state"},
            "protocol_reference": set(_PROTOCOL_REFERENCE_ROLES),
        }.get(node_type)
        if expected is None:
            if node_type in ("state_change", "same_state"):
                if not ({"parent_state", "operation"} <= role_set
                        <= _CONVENTION_ROLES):
                    return "proof_dag_premise_roles_mismatch"
            else:
                return "proof_dag_node_mismatch"
        elif role_set != expected:
            return "proof_dag_premise_roles_mismatch"
        # Premises verify before the node that stands on them.
        for premise in node["premises"]:
            issue = self._verify_node(
                premise["node_id"], nodes, memo, visiting, depth + 1,
            )
            if issue:
                return issue
        if node_type == "paper_literal":
            return self._verify_paper_literal(node)
        if node_type == "source_relation":
            return self._verify_source_relation(node, nodes)
        if node_type == "protocol_reference":
            return self._verify_protocol_reference(node, nodes)
        if node_type in ("state_change", "same_state"):
            return self._verify_convention(node, nodes)
        return self._verify_inheritance(node, nodes)

    # -- leaves --------------------------------------------------------------

    def _projection_text(self) -> str:
        if self._projection is None:
            from reaserch_agent.route_pdf_quote_binding import (
                _project, normalize_pdf_quote_whitespace,
            )

            normalized = [
                normalize_pdf_quote_whitespace(text)
                for _locator, text in self.blocks or ()
            ]
            separators = [" "] * (len(normalized) - 1)
            self._projection, _starts = _project(
                list(range(len(normalized))), normalized, separators,
            )
        return self._projection

    def _relocate_leaf(self, leaf: Mapping[str, Any]) -> tuple[str | None, str]:
        """Re-extract the leaf's excerpt from the signed blocks alone."""
        locator = _text(leaf.get("locator"))
        span = leaf.get("char_span")
        if (not locator or not isinstance(span, list) or len(span) != 2
                or any(not isinstance(offset, int) for offset in span)):
            return None, "proof_dag_leaf_not_relocatable"
        projection = self._projection_text()
        start, end = span
        if not 0 <= start < end <= len(projection):
            return None, "proof_dag_leaf_relocation_mismatch"
        text = projection[start:end]
        if _digest_text(text) != leaf.get("excerpt_digest"):
            return None, "proof_dag_leaf_relocation_mismatch"
        located = None
        if self.span_of is not None:
            locate = getattr(self.span_of, "locate", None)
            located = locate(text) if callable(locate) else None
        if (located is None or located[1] != locator
                or [located[2][0], located[2][1]] != [start, end]):
            return None, "proof_dag_leaf_relocation_mismatch"
        return text, ""

    def _verify_paper_literal(self, node: Mapping[str, Any]) -> str:
        leaf = node.get("leaf")
        if not isinstance(leaf, Mapping) or (
            leaf.get("schema_version") != SOURCE_EVIDENCE_LEAF_SCHEMA
        ):
            return "proof_dag_leaf_mismatch"
        if leaf_id_for(leaf) != leaf.get("leaf_id"):
            return "proof_dag_leaf_id_mismatch"
        if any(leaf.get(key) != self.context[key] for key in (
            "paper_id", "experimental_group_id", "source_digest",
        )):
            return "proof_dag_context_mismatch"
        fact = self.by_path.get(_text(leaf.get("field_path")))
        if fact is None or _fact_digest(fact) != leaf.get("fact_digest"):
            return "proof_dag_leaf_fact_mismatch"
        gates = node.get("gates")
        if not isinstance(gates, Mapping):
            return "proof_dag_gate_mismatch"
        excerpt_text: str | None = None
        if self.blocks is not None:
            excerpt_text, issue = self._relocate_leaf(leaf)
            if issue:
                return issue
        if excerpt_text is None:
            # Without the signed blocks only internal consistency is
            # verifiable; the leaf stays not source-verified.
            return ""
        value = leaf.get("claim_value")
        if (isinstance(value, str) and value
                and value.casefold() not in excerpt_text.casefold()):
            return "proof_dag_leaf_claim_mismatch"
        return self._verify_gates(node, leaf, excerpt_text)

    def _verify_gates(
        self, node: Mapping[str, Any], leaf: Mapping[str, Any],
        excerpt_text: str,
    ) -> str:
        field_path = _text(leaf.get("field_path"))
        value = _text(leaf.get("claim_value"))
        gates = node["gates"]
        if (_OUTPUT_STATE.fullmatch(field_path) is not None
                or _INPUT_STATE.fullmatch(field_path) is not None):
            port = _port_at(self.graph, field_path)
            if port is None:
                return "proof_dag_gate_mismatch"
            actual = _literal_state_gates(field_path, value, excerpt_text, port)
            expected = (
                {"controlled_state_mapping": True, "local_attribution": True}
                if actual is not None else None
            )
        else:
            if _OPERATION_PATH.fullmatch(field_path) is not None:
                passed = _literal_in_quote(value, excerpt_text)
            else:
                passed = bool(value) and (
                    value.casefold() in excerpt_text.casefold())
            expected = {"value_literal_in_quote": True} if passed else None
        if expected is None or dict(gates) != expected:
            return "proof_dag_gate_mismatch"
        return ""

    # -- composite nodes -----------------------------------------------------

    def _verify_source_relation(
        self, node: Mapping[str, Any], nodes: Mapping[str, Any],
    ) -> str:
        if node.get("subtype") != "post_operation_retained_object":
            return "proof_dag_node_mismatch"
        if self.span_of is None:
            return "proof_dag_resolver_missing"
        record = node.get("record")
        if not isinstance(record, Mapping):
            return "proof_dag_node_mismatch"
        recomputed, issue = derive_post_operation_retained_object(
            self.graph, self.facts, record.get("step_index"),
            span_of=self.span_of,
        )
        if recomputed is None:
            return issue or "proof_dag_node_mismatch"
        if recomputed != record:
            return "proof_dag_node_mismatch"
        role_fact_ids = {
            "operation": _text(record.get("operation_fact_id")),
            "naming": _text(record.get("naming_fact_id")),
            "output_name": _text(record.get("output_name_fact_id")),
        }
        for premise in node["premises"]:
            premise_node = nodes[premise["node_id"]]
            if premise_node.get("node_type") != "paper_literal":
                return "proof_dag_node_mismatch"
            fact = self.by_fact_id.get(role_fact_ids[premise["role"]])
            leaf = _mapping(premise_node.get("leaf"))
            if (fact is None
                    or leaf.get("fact_digest") != _fact_digest(fact)):
                return "proof_dag_node_mismatch"
        return ""

    def _verify_protocol_reference(
        self, node: Mapping[str, Any], nodes: Mapping[str, Any],
    ) -> str:
        """Recompute a protocol_reference node under the pinned context.

        Both premises must be paper literals whose leaf fact digests match
        the named definition/reference evidence facts (the same role-fact
        binding pattern as ``_verify_source_relation``); extraction and
        resolution are then recomputed from the signed blocks and facts,
        and every typed node field must equal the recomputed record.
        """
        if self.span_of is None:
            return "proof_dag_resolver_missing"
        claim = _mapping(node.get("claim"))
        operation_path = _text(claim.get("field_path"))
        match = _OPERATION_PATH.fullmatch(operation_path)
        if match is None or int(match.group(1)) >= len(self.graph):
            return "proof_dag_protocol_reference_mismatch"
        # The claim binds the operation this node certifies: no state,
        # no material identity — never a retained-object proposition.
        if any(_text(claim.get(key)) for key in (
            "target_state", "material_id", "material_instance_id",
        )):
            return "proof_dag_protocol_reference_mismatch"
        if (node.get("rule_id") != PROTOCOL_REFERENCE_RULE_ID
                or _text(node.get("rule_version"))
                != PROTOCOL_REFERENCE_RULE_VERSION
                or _text(node.get("rule_resource_digest")) != ""
                or _text(node.get("operation_path")) != operation_path):
            return "proof_dag_protocol_reference_mismatch"
        if any(node.get(key) != self.context[key] for key in (
            "paper_id", "experimental_group_id", "source_digest",
        )):
            return "proof_dag_context_mismatch"
        reference_fact = self.by_path.get(operation_path)
        if reference_fact is None:
            return "proof_dag_protocol_reference_mismatch"
        reference_evidence_id = _evidence_id(
            self.paper_id, self.experimental_group_id,
            _text(reference_fact.get("fact_id")))
        if (_text(node.get("reference_evidence_id")) != reference_evidence_id
                or _text(node.get("operation_value"))
                != _text(reference_fact.get("value"))):
            return "proof_dag_protocol_reference_mismatch"
        definition_fact = self.evidence.get(
            _text(node.get("definition_evidence_id")))
        if definition_fact is None:
            return "proof_dag_protocol_reference_mismatch"
        role_facts = {
            "definition_evidence": definition_fact,
            "reference_evidence": reference_fact,
        }
        for premise in node["premises"]:
            premise_node = nodes[premise["node_id"]]
            if premise_node.get("node_type") != "paper_literal":
                return "proof_dag_protocol_reference_mismatch"
            fact = role_facts[premise["role"]]
            leaf = _mapping(premise_node.get("leaf"))
            if leaf.get("fact_digest") != _fact_digest(fact):
                return "proof_dag_protocol_reference_mismatch"
        locate = getattr(self.span_of, "locate", None)
        located = (
            locate(_text(reference_fact.get("excerpt")))
            if callable(locate) else None
        )
        if located is None:
            return "proof_dag_protocol_reference_mismatch"
        record, issue = resolve_protocol_reference(
            _text(reference_fact.get("value")),
            _text(reference_fact.get("excerpt")),
            reference_evidence_id=reference_evidence_id,
            reference_locator=located[1],
            reference_position=located[2][0],
            candidates=_protocol_reference_candidates(
                self.facts, paper_id=self.paper_id,
                experimental_group_id=self.experimental_group_id,
                source_digest=self.source_digest, span_of=self.span_of,
            ),
            paper_id=self.paper_id,
            experimental_group_id=self.experimental_group_id,
            source_digest=self.source_digest,
        )
        if record is None:
            return issue or "proof_dag_protocol_reference_mismatch"
        comparisons = {
            "protocol_name": _text(record.get("protocol_name")),
            "operation_sequence": record.get("operation_sequence"),
            "liquid_medium": _text(record.get("liquid_medium")),
            "definition_evidence_id": _text(
                record.get("definition_evidence_id")),
            "definition_ordinal_anchor": _text(
                record.get("definition_ordinal_anchor")),
            "reference_ordinal": _text(record.get("reference_ordinal")),
            "definition_execution_count": _text(
                record.get("definition_execution_count")),
            "reference_execution_count": _text(
                record.get("reference_execution_count")),
            "definition_digest": _text(record.get("definition_digest")),
        }
        if any(node.get(key) != value for key, value in comparisons.items()):
            return "proof_dag_protocol_reference_mismatch"
        return ""

    def _verify_convention(
        self, node: Mapping[str, Any], nodes: Mapping[str, Any],
    ) -> str:
        claim = _mapping(node.get("claim"))
        field_path = _text(claim.get("field_path"))
        # Premises have already verified by recursion here.  Bind first:
        # the parent_state premise's claim must be exactly the parent-state
        # proposition this node's flat proof references — a substituted
        # otherwise-valid node fails even with every content address
        # honestly resealed, as does the correct ancestor at the wrong
        # chain level (parent_state_path is this step's own input path).
        premise_node = nodes[_role_premise(node, "parent_state")["node_id"]]
        binding_issue = _parent_state_binding_issue(
            premise_node,
            field_path=_text(node.get("parent_state_path")),
            material_instance_id=_text(node.get("parent_instance_id")),
            target_state=_text(node.get("parent_source_value")),
        )
        if binding_issue:
            return binding_issue
        roles = {premise["role"] for premise in node["premises"]}
        if "retained_object" in roles:
            # Scope guard: the retained_object role is exclusively bound to
            # source_relation nodes.  A protocol_reference node — or any
            # other node type — substituted into the role is rejected even
            # when it verifies on its own.
            retained_premise = nodes[
                _role_premise(node, "retained_object")["node_id"]]
            if retained_premise.get("node_type") != "source_relation":
                return "proof_dag_retained_object_binding_mismatch"
        liquid_token: Any = None
        if "liquid_medium" in roles:
            # The liquid_medium premise must be a protocol_reference node
            # bound to THIS step's operation (path, value and evidence id)
            # with a non-empty inherited medium; the flat recompute then
            # receives the capability token exactly like the parent-state
            # token flow, and the engine re-checks the binding.
            medium_premise = nodes[
                _role_premise(node, "liquid_medium")["node_id"]]
            step_match = _OUTPUT_STATE.fullmatch(field_path)
            step = (
                _mapping(self.graph[int(step_match.group(1))])
                if step_match is not None
                and int(step_match.group(1)) < len(self.graph) else {}
            )
            binding_issue = _liquid_medium_binding_issue(
                medium_premise,
                operation_path=_text(node.get("operation_path")),
                operation_evidence_id=_text(node.get("operation_evidence_id")),
                operation_value=_text(step.get("operation")),
            )
            if binding_issue:
                return binding_issue
            liquid_token = _mint_verified_liquid_token(self, medium_premise)
        # The flat engine's literal parent gate is discharged by the DAG
        # through a capability token minted STRUCTURALLY from the verified
        # premise node (this verifier owns it: the node verified clean by
        # recursion above); the engine re-checks the triple against its
        # own computed parent binding and fails closed on any mismatch.
        # The liquid token discharges only the liquid-participation gate,
        # and only for this step's exact operation binding.  Both tokens
        # pass the exact-type guard at this derive boundary.
        proof, issue = derive_unreviewed_output_state(
            self.graph, self.facts, field_path,
            paper_id=self.paper_id,
            experimental_group_id=self.experimental_group_id,
            source_digest=self.source_digest,
            retained_object_resolver=self.resolver,
            verified_parent_state=_assert_verified_parent_token(
                _mint_verified_parent_token(self, premise_node)),
            verified_liquid_medium=_assert_verified_liquid_token(liquid_token),
        )
        if proof is None:
            return issue or "proof_dag_node_mismatch"
        rule_id = _text(proof.get("rule_id"))
        expected_type = (
            "same_state" if rule_id in _RULE_EVENTS else "state_change"
        )
        if node.get("node_type") != expected_type:
            return "proof_dag_node_mismatch"
        retained = {
            key: proof[key] for key in proof if key.startswith("retained_object")
        }
        roles = {premise["role"] for premise in node["premises"]}
        if ("retained_object" in roles) != bool(retained):
            return "proof_dag_node_mismatch"
        comparisons = {
            "rule_id": rule_id,
            "rule_version": _text(proof.get("rule_version")),
            "rule_resource_digest": _text(proof.get("resource_digest")),
            "relation_id": _text(proof.get("relation_id")),
            "parent_instance_id": _text(proof.get("parent_instance_id")),
            "child_instance_id": _text(proof.get("child_instance_id")),
            "parent_state_path": _text(proof.get("parent_state_path")),
            "parent_source_value": _text(proof.get("parent_source_value")),
            "parent_evidence_id": _text(proof.get("parent_evidence_id")),
            "operation_path": _text(proof.get("operation_path")),
            "operation_evidence_id": _text(proof.get("operation_evidence_id")),
        }
        if any(node.get(key) != value for key, value in comparisons.items()):
            return "proof_dag_node_mismatch"
        if _text(claim.get("target_state")) != _text(proof.get("target_state")):
            return "proof_dag_node_mismatch"
        if node.get("retained_object_fields", {}) != retained:
            return "proof_dag_node_mismatch"
        # The segment id binds the node to the graph relation it names.
        match = _OUTPUT_STATE.fullmatch(field_path)
        if match is None or int(match.group(1)) >= len(self.graph):
            return "proof_dag_node_mismatch"
        relation = next(
            (item for item in _items(
                _mapping(self.graph[int(match.group(1))]).get("material_relations"))
             if _text(item.get("relation_id")) == comparisons["relation_id"]),
            None,
        )
        if relation is None or _text(node.get("segment_id")) != _text(
            relation.get("source_operation_ref")
        ):
            return "proof_dag_node_mismatch"
        return ""

    def _verify_inheritance(
        self, node: Mapping[str, Any], nodes: Mapping[str, Any],
    ) -> str:
        claim = _mapping(node.get("claim"))
        field_path = _text(claim.get("field_path"))
        parent_ref = node.get("parent_ref")
        if (not isinstance(parent_ref, Mapping)
                or parent_ref.get("kind") != "material_instance"
                or not _text(parent_ref.get("macro_step_id"))
                or not _text(parent_ref.get("material_instance_id"))):
            return "proof_dag_parent_ref_mismatch"
        match = _INPUT_STATE.fullmatch(field_path)
        if match is None:
            return "proof_dag_node_mismatch"
        step_index, collection, port_index = (
            int(match.group(1)), match.group(2), int(match.group(3)),
        )
        if step_index >= len(self.graph):
            return "proof_dag_node_mismatch"
        ports = _items(self.graph[step_index].get(collection))
        if port_index >= len(ports):
            return "proof_dag_node_mismatch"
        port = ports[port_index]
        references = port.get("parent_output_refs")
        if (not isinstance(references, list) or len(references) != 1
                or _text(_mapping(references[0]).get("macro_step_id"))
                != _text(parent_ref.get("macro_step_id"))
                or _text(_mapping(references[0]).get("material_instance_id"))
                != _text(parent_ref.get("material_instance_id"))):
            return "proof_dag_parent_ref_mismatch"
        matches = _upstream_matches(self.graph, parent_ref)
        if matches and all(index >= step_index for index in matches):
            return "proof_dag_future_reference"
        premise = node["premises"][0]
        parent_node = nodes[premise["node_id"]]
        if parent_node.get("node_type") not in ("state_change", "same_state"):
            return "proof_dag_node_mismatch"
        # Bind the premise claim to the typed parent_ref: resolve the
        # reference to the matching upstream output port and require the
        # premise to claim exactly that port's state path, the referenced
        # material instance id, and the carried-forward state value.  Per
        # the inheritance record's semantics the record's target_state is
        # the input port's state, which the engine constrains to equal the
        # upstream output port's state; the premise node proves the
        # upstream output state, so the resolved port's own ``state`` value
        # is the proposition bound here (it coincides with this node's
        # claim.target_state).
        ref_step, ref_output, ref_port = _resolve_parent_output(
            self.graph, step_index, parent_ref,
        )
        if ref_port is None:
            return "proof_dag_parent_ref_mismatch"
        binding_issue = _parent_state_binding_issue(
            parent_node,
            field_path=(
                f"material_graph[{ref_step}]"
                f".material_outputs[{ref_output}].state"
            ),
            material_instance_id=_text(parent_ref.get("material_instance_id")),
            target_state=_text(ref_port.get("state")),
        )
        if binding_issue:
            return binding_issue
        # The capability token certifies the GRANDPARENT binding — the
        # parent output node's own parent_state premise claim, itself
        # verified by recursion — so a non-literal grandparent state needs
        # no literal gate in the re-derived parent proof; chains of more
        # than two consecutive non-literal hops re-derive through this
        # recursion alone.  Minted structurally: this verifier owns the
        # premise node, which verified clean by recursion above.
        verified_grandparent = _mint_verified_parent_token(
            self, nodes[_role_premise(parent_node, "parent_state")["node_id"]])
        grandparent_fact = self.evidence.get(
            _text(parent_node.get("parent_evidence_id")))
        operation_fact = self.evidence.get(
            _text(parent_node.get("operation_evidence_id")))
        record, issue = _inheritance_proof_for_evidence(
            self.graph, field_path,
            parent_source_value=_text(parent_node.get("parent_source_value")),
            parent_excerpt=(_text(grandparent_fact.get("excerpt"))
                            if grandparent_fact is not None else ""),
            parent_evidence_id=_text(parent_node.get("parent_evidence_id")),
            operation_excerpt=(_text(operation_fact.get("excerpt"))
                               if operation_fact is not None else ""),
            operation_evidence_id=_text(parent_node.get("operation_evidence_id")),
            paper_id=self.paper_id,
            experimental_group_id=self.experimental_group_id,
            source_digest=self.source_digest,
            retained_object_resolver=self.resolver,
            facts=self.facts,
            verified_parent_state=_assert_verified_parent_token(
                verified_grandparent),
        )
        if record is None:
            return issue or "proof_dag_node_mismatch"
        comparisons = {
            "rule_id": _INHERITANCE_RULE_ID,
            "rule_version": _text(record.get("rule_version")),
            "parent_instance_id": _text(record.get("parent_instance_id")),
            "child_instance_id": _text(record.get("child_instance_id")),
            "parent_state_path": _text(record.get("parent_state_path")),
            "parent_source_value": _text(record.get("parent_source_value")),
            "parent_evidence_id": _text(record.get("parent_evidence_id")),
            "operation_path": _text(record.get("operation_path")),
            "operation_evidence_id": _text(record.get("operation_evidence_id")),
            "relation_id": _text(record.get("relation_id")),
        }
        if any(node.get(key) != value for key, value in comparisons.items()):
            return "proof_dag_node_mismatch"
        if _text(claim.get("target_state")) != _text(record.get("target_state")):
            return "proof_dag_node_mismatch"
        return ""


def verify_state_proof_dag(
    dag: Any, graph: Sequence[Any], facts: Sequence[Any], *, paper_id: str,
    experimental_group_id: str, source_digest: str, span_of: Any = None,
    blocks: Sequence[Any] = None, caption_block_locators: Sequence[str] = (),
) -> str:
    """Convenience wrapper: one verifier, one pinned context, one issue str."""
    return StateProofDagVerifier(
        graph, facts, paper_id=paper_id,
        experimental_group_id=experimental_group_id,
        source_digest=source_digest, span_of=span_of, blocks=blocks,
        caption_block_locators=caption_block_locators,
    ).verify(dag)


__all__ = [
    "PAPER_LITERAL_PROOF_SCHEMA", "PROTOCOL_REFERENCE_PROOF_SCHEMA",
    "SAME_STATE_CONVENTION_PROOF_SCHEMA",
    "SOURCE_EVIDENCE_LEAF_SCHEMA", "SOURCE_RELATION_PROOF_SCHEMA",
    "STATE_CHANGE_CONVENTION_PROOF_SCHEMA", "STATE_INHERITANCE_PROOF_SCHEMA",
    "STATE_PROOF_DAG_SCHEMA", "StateProofDagVerifier",
    "build_state_proof_dag", "leaf_id_for", "node_id_for",
    "verify_state_proof_dag",
]
