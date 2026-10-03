"""Round 3E: operation-precondition-inference feasibility diagnostics.

Diagnostics-only evaluator for the chartered feasibility study
(``docs/field_semantic_gate_3e_design_20261003.md``).  The question under
study is whether a downstream operation's preconditions can ever diagnose
the state of an upstream output — concretely, whether ``ms7b`` (the
redispersion segment of the second centrifugation−redispersion protocol)
can diagnose ``ms7a.out`` (the retained phase of the second
centrifugation).  The answer is a diagnostic record, never a proof:

- three propositions are answered SEPARATELY — P1 (necessary input
  condition: an INDEPENDENT necessity basis is required; a
  retained-phase statement supplies upstream state evidence but does
  not by itself prove the state is a NECESSARY input of the
  redispersion), P2 (this material flow), P3 (material instance
  binding) — and an operation name or an edge the proposal drew itself
  can never answer any of them;
- source identity (``paper_explicit`` / ``supplement_explicit`` /
  ``external_primary``) is recorded separately from the inference nature
  (``direct_evidence`` / ``rule_compatible_states`` /
  ``proposal_assertion`` / ``assumption``);
- the forward whitelist (REDISPERSION_V1 ``allowed_input_states``) may
  only appear as ``rule_compatible_states`` — it defines rule
  applicability and proves neither set membership nor WHICH member
  (inverting it into a necessity basis is rejected as
  ``inversion_rejected``);
- the evaluator builds its OWN dependency view from the proposal graph
  and rejects any candidate model whose support includes a downstream
  node it feeds (``circular_dependency_rejected``) — ``ms7b.in`` already
  depends on ``ms7a.out``, so downstream state may never be assumed in
  order to prove the upstream;
- an item submitted as ``direct_evidence`` must carry a checkable
  identity — non-empty content, a real source identity
  (paper/supplement/external), its own scope (paper / group / stage),
  and a provenance — or it is rejected
  (``evidence_identity_missing_rejected``) before any other check; rule
  whitelists, proposal assertions, and assumptions carry no source
  identity by design and are exempt from this gate.  The gate
  deliberately does NOT require the evidence invocation: an observation
  record may legitimately bind no invocation — that case is handled by
  ``invocation_unbound_rejected`` below, never by fabricating one;
- scope is explicit everywhere: cross-paper/cross-group evidence is
  rejected (``scope_mismatch_rejected``), same-group cross-stage
  evidence is rejected (``stage_mismatch_rejected``), first-invocation
  results applied to a second invocation are rejected
  (``invocation_swap_rejected``), and evidence individuating a different
  material instance is rejected (``binding_mismatch_rejected``);
- the scope under diagnosis must itself be COMPLETE:
  ``evaluate_candidate_model`` raises ``ValueError`` unless the model
  scope binds all four fields (paper_id / experimental_group_id / stage
  / invocation — pure whitespace counts as missing); the diagnostic
  target of this study always binds the protocol-invocation ordinal
  (``"second"``), and an unknown scope is never a matching scope.
  Symmetrically, a ``direct_evidence`` item that binds NO protocol
  invocation (blank ``scope.invocation``) is rejected
  (``invocation_unbound_rejected``) — an unbound invocation cannot
  support an invocation-bound target; the check fires AFTER the stage
  check, so a true cross-stage observation record (whose empty
  invocation is a real attribute) keeps ``stage_mismatch_rejected``;
- sources are content-addressed: when a live source is mutated, every
  diagnostic item citing it is recomputed or invalidated honestly — no
  stale citation survives.  The citation check is OPT-IN via
  ``live_sources``: when an item's provenance is not among the supplied
  live sources, no source-content verification is claimed for that item
  — its citation-check status (checked / not checkable / stale) stays
  distinct from the proposition-support relation, which remains the
  submitter's annotation until the citation is actually checked.

Fixed constraints (owner-locked): every record carries
``diagnostics_only=True`` and ``feeds_verdict=False``; this module mints
NO token of any kind (neither verified capability tokens nor
diagnostic-assumption tokens); it derives nothing with the engine; the
evaluated node stays BLOCKED — the diagnostic never changes node
verdicts.  The conclusion vocabulary is exactly ``insufficient``
(default whenever any proposition is unproven) and
``conditional_constraint`` (the ceiling for a model standing on recorded
assumptions only); even that ceiling stays ``non_unique`` while the
compatible-state set has more than one member.

Pure functions and frozen data classes only.  This module is a
diagnostic prototype living in ``reaserch_agent/``; nothing here is a
proof class, and nothing here touches the contracts package.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
import json

SCHEMA_VERSION = "operation-precondition-diagnostic/v1"

# ---------------------------------------------------------------------------
# Vocabulary (charter-fixed).
# ---------------------------------------------------------------------------

# Source identity — WHERE an item comes from.  Recorded separately from
# the inference nature.
PAPER_EXPLICIT = "paper_explicit"
SUPPLEMENT_EXPLICIT = "supplement_explicit"
EXTERNAL_PRIMARY = "external_primary"
SOURCE_IDENTITIES = (PAPER_EXPLICIT, SUPPLEMENT_EXPLICIT, EXTERNAL_PRIMARY)
#: Rule whitelists and proposal-internal artifacts carry no source
#: identity; the field stays empty for them.
NO_SOURCE = ""

# Inference nature — HOW an item is allowed to support a proposition.
DIRECT_EVIDENCE = "direct_evidence"
RULE_COMPATIBLE_STATES = "rule_compatible_states"
PROPOSAL_ASSERTION = "proposal_assertion"
ASSUMPTION = "assumption"
INFERENCE_NATURES = (
    DIRECT_EVIDENCE,
    RULE_COMPATIBLE_STATES,
    PROPOSAL_ASSERTION,
    ASSUMPTION,
)

# The three propositions (each answered separately).
P1_NECESSARY_INPUT_CONDITION = "necessary_input_condition"
P2_THIS_MATERIAL_FLOW = "this_material_flow"
P3_MATERIAL_INSTANCE_BINDING = "material_instance_binding"
PROPOSITIONS = (
    P1_NECESSARY_INPUT_CONDITION,
    P2_THIS_MATERIAL_FLOW,
    P3_MATERIAL_INSTANCE_BINDING,
)

# The subject an item must speak to in order to qualify as DIRECT support
# for a proposition.  An operation-name mention speaks to the operation's
# occurrence, never to these subjects.
PROPOSITION_SUBJECTS = {
    P1_NECESSARY_INPUT_CONDITION: "necessary_input_condition",
    P2_THIS_MATERIAL_FLOW: "inter_segment_material_flow",
    P3_MATERIAL_INSTANCE_BINDING: "material_instance_identity",
}

# Per-proposition verdicts.
PROVEN = "proven"
UNPROVEN = "unproven"
PROPOSITION_VERDICTS = (PROVEN, UNPROVEN)

# Conclusion vocabulary (charter calibration expectation).
INSUFFICIENT = "insufficient"
CONDITIONAL_CONSTRAINT = "conditional_constraint"
CONCLUSIONS = (INSUFFICIENT, CONDITIONAL_CONSTRAINT)

# Rejection codes.
INVERSION_REJECTED = "inversion_rejected"
NON_UNIQUE = "non_unique"
BINDING_MISMATCH_REJECTED = "binding_mismatch_rejected"
INVOCATION_SWAP_REJECTED = "invocation_swap_rejected"
SCOPE_MISMATCH_REJECTED = "scope_mismatch_rejected"
STAGE_MISMATCH_REJECTED = "stage_mismatch_rejected"
INVOCATION_UNBOUND_REJECTED = "invocation_unbound_rejected"
EVIDENCE_IDENTITY_MISSING_REJECTED = "evidence_identity_missing_rejected"
CIRCULAR_DEPENDENCY_REJECTED = "circular_dependency_rejected"
STALE_SOURCE_INVALIDATED = "stale_source_invalidated"
OVER_CLAIM_REJECTED = "over_claim_rejected"
REJECTION_CODES = (
    INVERSION_REJECTED,
    NON_UNIQUE,
    BINDING_MISMATCH_REJECTED,
    INVOCATION_SWAP_REJECTED,
    SCOPE_MISMATCH_REJECTED,
    STAGE_MISMATCH_REJECTED,
    INVOCATION_UNBOUND_REJECTED,
    EVIDENCE_IDENTITY_MISSING_REJECTED,
    CIRCULAR_DEPENDENCY_REJECTED,
    STALE_SOURCE_INVALIDATED,
    OVER_CLAIM_REJECTED,
)

#: Fixed-constraints block (owner-locked).  Every record carries exactly
#: these values; the diagnostic never feeds a verdict.
DIAGNOSTICS_ONLY = True
FEEDS_VERDICT = False


def evidence_content_digest(content: str) -> str:
    """Content address of one evidence item's content."""
    return "sha256_" + sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Data classes (frozen; the record is a value, not a process).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeBindingV1:
    """The experimental scope a node or an evidence item belongs to.

    ``invocation`` is the protocol-invocation ordinal the evidence
    belongs to (e.g. ``"first"`` / ``"second"``); stage is the proposal
    macro step id.  Evidence whose scope disagrees with the scope under
    diagnosis is rejected, never silently re-scoped.
    """

    paper_id: str
    experimental_group_id: str
    stage: str
    invocation: str


@dataclass(frozen=True)
class SourceRefV1:
    """Diagnostic-record field (1): source and figure-snapshot digest."""

    paper_id: str
    experimental_group_id: str
    source_digest: str
    figure_snapshot_digest: str = ""


@dataclass(frozen=True)
class EvidenceItemV1:
    """One diagnostic evidence item.

    ``source_identity`` (where it comes from) is recorded separately from
    ``inference_nature`` (how it may support a proposition).  ``subject``
    is what the item actually speaks to; only an item whose
    ``inference_nature`` is ``direct_evidence`` AND whose ``subject`` is
    exactly the proposition's required subject can qualify as direct
    support.  ``material_instance_id`` is the instance the item
    individuates (empty = non-individuating / collective).  ``scope`` is
    ``None`` only for scope-free artifacts (a rule whitelist); any
    paper/SI/external item must carry its scope.
    ``presented_as_necessity_basis`` records that the MODEL presents the
    item as proving necessity — presenting a ``rule_compatible_states``
    item that way is the inversion over-claim.
    """

    content: str
    source_identity: str
    inference_nature: str
    provenance: str = ""
    subject: str = ""
    scope: ScopeBindingV1 | None = None
    material_instance_id: str = ""
    state_path: str = ""
    presented_as_necessity_basis: bool = False

    @property
    def content_digest(self) -> str:
        return evidence_content_digest(self.content)


@dataclass(frozen=True)
class PropositionClaimV1:
    """The model's submission for one proposition: evidence, assumptions,
    and the verdict the model asserts (an asserted ``proven`` that the
    evaluator cannot re-derive is an over-claim, never a pass)."""

    proposition: str
    evidence: tuple[EvidenceItemV1, ...] = ()
    assumptions: tuple[str, ...] = ()
    asserted_verdict: str = UNPROVEN


@dataclass(frozen=True)
class CandidateModelV1:
    """A candidate state model under diagnosis.

    ``compatible_states`` is the rule-compatible set the model draws on
    (the forward whitelist, recorded as ``rule_compatible_states``); a
    set with more than one member can never single out
    ``candidate_state`` by itself.
    """

    target_state_path: str
    candidate_state: str
    scope: ScopeBindingV1
    target_material_instance_id: str
    propositions: tuple[PropositionClaimV1, ...] = ()
    compatible_states: tuple[str, ...] = ()
    recorded_assumptions: tuple[str, ...] = ()
    node_verdict: str = "BLOCKED"


@dataclass(frozen=True)
class DependencyViewV1:
    """The evaluator's own dependency view over the proposal graph.

    ``parents`` maps a state path to the state paths it directly depends
    on; ``downstream`` maps a state path to the state paths that directly
    depend on it.  Built from ``parent_output_refs`` plus the within-step
    input→output edge; nothing is derived with the engine.
    """

    parents: tuple[tuple[str, tuple[str, ...]], ...] = ()
    downstream: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def parents_of(self, state_path: str) -> tuple[str, ...]:
        return dict(self.parents).get(state_path, ())

    def downstream_of(self, state_path: str) -> tuple[str, ...]:
        return dict(self.downstream).get(state_path, ())

    def downstream_closure(self, state_path: str) -> tuple[str, ...]:
        """Every state path that transitively depends on ``state_path``."""
        downstream = dict(self.downstream)
        seen: set[str] = set()
        stack = list(downstream.get(state_path, ()))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(downstream.get(current, ()))
        return tuple(sorted(seen))


@dataclass(frozen=True)
class RejectionV1:
    code: str
    proposition: str
    detail: str
    provenance: str = ""


@dataclass(frozen=True)
class PropositionDiagnosisV1:
    """Diagnostic-record field (3), per proposition."""

    proposition: str
    evidence: tuple[EvidenceItemV1, ...]
    assumptions: tuple[str, ...]
    verdict: str
    asserted_verdict: str
    qualifying_evidence: tuple[EvidenceItemV1, ...] = ()


@dataclass(frozen=True)
class DependencyRelationV1:
    """Diagnostic-record field (5): one recorded dependency relation."""

    dependent: str
    depends_on: str
    kind: str
    note: str = ""


@dataclass(frozen=True)
class OpenItemV1:
    """Diagnostic-record field (6): what evidence would close one
    proposition."""

    proposition: str
    needed_evidence: str


@dataclass(frozen=True)
class ConstraintsV1:
    """Diagnostic-record field (7): the owner-locked constraints block."""

    diagnostics_only: bool = DIAGNOSTICS_ONLY
    feeds_verdict: bool = FEEDS_VERDICT

    def __post_init__(self) -> None:
        if self.diagnostics_only is not DIAGNOSTICS_ONLY:
            raise ValueError("constraints block violated: diagnostics_only "
                             "must stay true")
        if self.feeds_verdict is not FEEDS_VERDICT:
            raise ValueError("constraints block violated: feeds_verdict "
                             "must stay false")


@dataclass(frozen=True)
class DiagnosticRecordV1:
    """The charter paper-v1 diagnostic record (7 fields + conclusion)."""

    schema_version: str
    source: SourceRefV1
    scope: ScopeBindingV1
    target_state_path: str
    candidate_state: str
    propositions: tuple[PropositionDiagnosisV1, ...]
    alternative_explanations: tuple[str, ...]
    dependency_relations: tuple[DependencyRelationV1, ...]
    open_items: tuple[OpenItemV1, ...]
    constraints: ConstraintsV1
    conclusion: str
    assumption_only_model: Mapping[str, Any]
    rejections: tuple[RejectionV1, ...] = ()
    invalidated_items: tuple[RejectionV1, ...] = ()
    node_verdict_unchanged: str = "BLOCKED"
    record_digest: str = ""


# ---------------------------------------------------------------------------
# The evaluator's own dependency view (pure; reads the proposal graph).
# ---------------------------------------------------------------------------


def _output_state_paths(material_graph: Sequence[Mapping[str, Any]]) -> dict:
    """(macro_step_id, material_instance_id) -> output state path."""
    by_instance: dict[tuple[str, str], str] = {}
    for step_index, step in enumerate(material_graph):
        macro_step_id = step.get("macro_step_id", "")
        for output_index, output in enumerate(
                step.get("material_outputs", []) or []):
            by_instance[(macro_step_id,
                         output.get("material_instance_id", ""))] = (
                f"material_graph[{step_index}].material_outputs"
                f"[{output_index}].state")
    return by_instance


def build_dependency_view(
        material_graph: Sequence[Mapping[str, Any]]) -> DependencyViewV1:
    """Build the evaluator's own dependency view from the proposal graph.

    Edges: (a) ``parent_output_refs`` — a downstream input port depends on
    the upstream output port it names (macro_step_id + instance id);
    (b) within-step input→output — an output port depends on its own
    step's input ports.  Pure graph reading; no engine derive.
    """
    outputs_by_instance = _output_state_paths(material_graph)
    parents: dict[str, set[str]] = {}
    downstream: dict[str, set[str]] = {}

    def link(child: str, parent: str) -> None:
        parents.setdefault(child, set()).add(parent)
        downstream.setdefault(parent, set()).add(child)

    for step_index, step in enumerate(material_graph):
        input_paths: list[str] = []
        for input_index, port in enumerate(
                step.get("material_inputs", []) or []):
            input_path = (f"material_graph[{step_index}].material_inputs"
                          f"[{input_index}].state")
            input_paths.append(input_path)
            for ref in port.get("parent_output_refs", []) or []:
                parent_path = outputs_by_instance.get(
                    (ref.get("macro_step_id", ""),
                     ref.get("material_instance_id", "")))
                if parent_path:
                    link(input_path, parent_path)
        for output_index, _output in enumerate(
                step.get("material_outputs", []) or []):
            output_path = (f"material_graph[{step_index}].material_outputs"
                           f"[{output_index}].state")
            for input_path in input_paths:
                link(output_path, input_path)
    return DependencyViewV1(
        parents=tuple(sorted((path, tuple(sorted(deps)))
                             for path, deps in parents.items())),
        downstream=tuple(sorted((path, tuple(sorted(deps)))
                                for path, deps in downstream.items())))


# ---------------------------------------------------------------------------
# Item checks (order matters: the direct-evidence identity gate first,
# then staleness, then paper/group scope, invocation swap, stage, the
# invocation-binding check, then instance, then circularity, then
# inversion).
# ---------------------------------------------------------------------------


def _target_scope_missing_fields(scope: ScopeBindingV1 | None
                                 ) -> tuple[str, ...]:
    """The target-scope fields the model under diagnosis leaves blank.

    The scope under diagnosis must be COMPLETE — paper_id /
    experimental_group_id / stage / invocation all non-blank (pure
    whitespace counts as missing).  The diagnostic target of this study
    always binds the protocol-invocation ordinal (``"second"``): an
    unknown scope is not a matching scope, so an incomplete target scope
    is a ``ValueError``, never a silent pass of the scoped comparisons.
    """
    if scope is None:
        return ("scope",)
    missing: list[str] = []
    if not scope.paper_id.strip():
        missing.append("scope.paper_id")
    if not scope.experimental_group_id.strip():
        missing.append("scope.experimental_group_id")
    if not scope.stage.strip():
        missing.append("scope.stage")
    if not scope.invocation.strip():
        missing.append("scope.invocation")
    return tuple(missing)


def _identity_missing_fields(item: EvidenceItemV1) -> tuple[str, ...]:
    """The checkable-identity fields a ``direct_evidence`` item lacks.

    A direct-evidence submission must carry non-empty content, a real
    source identity (paper/supplement/external — never ``NO_SOURCE``),
    its own scope with paper / group / stage filled, and a provenance.
    Rule whitelists, proposal assertions, and assumptions carry no
    source identity by design; this gate applies to ``direct_evidence``
    only.
    """
    missing: list[str] = []
    if not item.content.strip():
        missing.append("content")
    if item.source_identity not in SOURCE_IDENTITIES:
        missing.append("source_identity")
    scope = item.scope
    if scope is None:
        missing.append("scope")
    else:
        if not scope.paper_id:
            missing.append("scope.paper_id")
        if not scope.experimental_group_id:
            missing.append("scope.experimental_group_id")
        if not scope.stage:
            missing.append("scope.stage")
    if not item.provenance.strip():
        missing.append("provenance")
    return tuple(missing)


def _check_item(item: EvidenceItemV1, proposition: str,
                model: CandidateModelV1,
                view: DependencyViewV1,
                live_sources: Mapping[str, str]) -> str:
    """One rejection code for one item, or ``""`` when the item stands."""
    if item.source_identity not in SOURCE_IDENTITIES + (NO_SOURCE,):
        raise ValueError(f"unknown source_identity {item.source_identity!r}")
    if item.inference_nature not in INFERENCE_NATURES:
        raise ValueError(f"unknown inference_nature {item.inference_nature!r}")
    if item.inference_nature == DIRECT_EVIDENCE \
            and _identity_missing_fields(item):
        return EVIDENCE_IDENTITY_MISSING_REJECTED
    if item.provenance and item.provenance in live_sources:
        live = live_sources[item.provenance]
        if evidence_content_digest(live) != item.content_digest:
            return STALE_SOURCE_INVALIDATED
    scope = item.scope
    if scope is not None:
        if (scope.paper_id != model.scope.paper_id
                or scope.experimental_group_id
                != model.scope.experimental_group_id):
            return SCOPE_MISMATCH_REJECTED
        if scope.invocation and model.scope.invocation \
                and scope.invocation != model.scope.invocation:
            return INVOCATION_SWAP_REJECTED
        if scope.stage and model.scope.stage \
                and scope.stage != model.scope.stage:
            return STAGE_MISMATCH_REJECTED
        # AFTER the stage check: a cross-stage observation record whose
        # empty invocation is a REAL attribute (e.g. the E10 caption)
        # must keep stage_mismatch_rejected and never reaches here.
        if item.inference_nature == DIRECT_EVIDENCE \
                and model.scope.invocation \
                and not scope.invocation.strip():
            return INVOCATION_UNBOUND_REJECTED
    if item.material_instance_id and model.target_material_instance_id \
            and item.material_instance_id \
            != model.target_material_instance_id:
        return BINDING_MISMATCH_REJECTED
    if item.state_path and item.state_path in view.downstream_closure(
            model.target_state_path):
        return CIRCULAR_DEPENDENCY_REJECTED
    if (proposition == P1_NECESSARY_INPUT_CONDITION
            and item.inference_nature == RULE_COMPATIBLE_STATES
            and item.presented_as_necessity_basis):
        return INVERSION_REJECTED
    return ""


def _qualifies(item: EvidenceItemV1, proposition: str,
               model: CandidateModelV1) -> bool:
    """Whether a standing item qualifies as DIRECT support.

    Only ``direct_evidence`` whose subject is exactly the proposition's
    required subject qualifies; P3 additionally requires the item to
    individuate the instance under proof (a collective statement never
    binds one instance).  An operation name, a proposal-drawn edge, a
    whitelist entry, or an assumption never qualifies.
    """
    if item.inference_nature != DIRECT_EVIDENCE:
        return False
    if item.subject != PROPOSITION_SUBJECTS.get(proposition):
        return False
    if proposition == P3_MATERIAL_INSTANCE_BINDING:
        return (bool(model.target_material_instance_id)
                and item.material_instance_id
                == model.target_material_instance_id)
    return True


#: Deterministic per-proposition wording for the derived open items.
_OPEN_ITEM_DEFAULTS = {
    P1_NECESSARY_INPUT_CONDITION: (
        "an independent necessity basis for the redispersion segment's "
        "necessary input — i.e. the upstream centrifugation output "
        "state under proof (a paper/SI statement of this invocation's "
        "retained phase supplies upstream state evidence, but does not "
        "by itself prove the state is a NECESSARY input of the "
        "redispersion) — not the operation name, not the forward "
        "whitelist"),
    P2_THIS_MATERIAL_FLOW: (
        "an explicit inter-segment material-flow statement binding this "
        "downstream input to THIS upstream output"),
    P3_MATERIAL_INSTANCE_BINDING: (
        "instance-individuating language (per-instance identity, not a "
        "collective 'all the samples' statement)"),
}


def evaluate_candidate_model(
        model: CandidateModelV1,
        *,
        dependency_view: DependencyViewV1,
        source: SourceRefV1,
        alternative_explanations: Sequence[str] = (),
        dependency_relations: Sequence[DependencyRelationV1] = (),
        open_item_overrides: Mapping[str, str] | None = None,
        live_sources: Mapping[str, str] | None = None,
        non_inversion_note: str = "") -> DiagnosticRecordV1:
    """Evaluate one candidate model into a diagnostic record.

    The evaluator re-derives every proposition verdict from the standing
    evidence; the model's asserted verdicts are claims, never passes.
    The conclusion is ``insufficient`` whenever any proposition is
    unproven; ``conditional_constraint`` is only ever the ceiling of a
    model standing on recorded assumptions alone (and it stays
    ``non_unique`` while the compatible-state set has more than one
    member).  The evaluated node's verdict is copied through unchanged —
    the diagnostic never changes it.

    Entry gate: the scope under diagnosis must be COMPLETE — paper_id /
    experimental_group_id / stage / invocation all non-blank (pure
    whitespace counts as missing) — or this raises ``ValueError``
    listing the missing fields, before any evidence is checked: an
    unknown scope is never a matching scope, and the diagnostic target
    of this study always binds the protocol-invocation ordinal.
    """
    missing_scope = _target_scope_missing_fields(model.scope)
    if missing_scope:
        raise ValueError(
            "candidate model scope is incomplete — the scope under "
            "diagnosis must bind paper_id / experimental_group_id / "
            "stage / invocation explicitly (an unknown scope is never a "
            "matching scope); missing: " + ", ".join(missing_scope))
    live = live_sources or {}
    overrides = dict(open_item_overrides or {})
    rejections: list[RejectionV1] = []
    invalidated: list[RejectionV1] = []
    diagnoses: list[PropositionDiagnosisV1] = []
    open_items: list[OpenItemV1] = []

    claims = {claim.proposition: claim for claim in model.propositions}
    for proposition in PROPOSITIONS:
        claim = claims.get(proposition) or PropositionClaimV1(proposition)
        standing: list[EvidenceItemV1] = []
        qualifying: list[EvidenceItemV1] = []
        for item in claim.evidence:
            code = _check_item(item, proposition, model,
                               dependency_view, live)
            if code:
                entry = RejectionV1(
                    code=code, proposition=proposition,
                    detail=_rejection_detail(code, item, model),
                    provenance=item.provenance)
                rejections.append(entry)
                if code == STALE_SOURCE_INVALIDATED:
                    invalidated.append(entry)
                continue
            standing.append(item)
            if _qualifies(item, proposition, model):
                qualifying.append(item)
        verdict = PROVEN if qualifying else UNPROVEN
        if claim.asserted_verdict == PROVEN and verdict == UNPROVEN \
                and not any(r.proposition == proposition
                            for r in rejections):
            rejections.append(RejectionV1(
                code=OVER_CLAIM_REJECTED, proposition=proposition,
                detail=("model asserts 'proven' but no standing evidence "
                        "qualifies as direct support")))
        diagnoses.append(PropositionDiagnosisV1(
            proposition=proposition,
            evidence=tuple(standing),
            assumptions=claim.assumptions,
            verdict=verdict,
            asserted_verdict=claim.asserted_verdict,
            qualifying_evidence=tuple(qualifying)))
        if verdict == UNPROVEN:
            open_items.append(OpenItemV1(
                proposition=proposition,
                needed_evidence=overrides.get(
                    proposition, _OPEN_ITEM_DEFAULTS[proposition])))

    verdicts = {d.proposition: d.verdict for d in diagnoses}
    if any(v == UNPROVEN for v in verdicts.values()):
        conclusion = INSUFFICIENT
    else:
        # Unreachable in this study: even with all three propositions
        # proven, the diagnostic vocabulary's ceiling is a conditional
        # constraint — the formal-proof gate (alternatives excluded,
        # counter-examples passing) is a separate evaluation.
        conclusion = CONDITIONAL_CONSTRAINT

    non_unique = len(model.compatible_states) > 1
    assumption_only = {
        "ceiling": CONDITIONAL_CONSTRAINT,
        "basis": ("recorded assumptions only — no proposition is proven "
                  "on direct evidence"),
        "compatible_states": list(model.compatible_states),
        "non_unique": non_unique,
        "non_unique_note": (
            "the compatible-state set has more than one member; even "
            "under the (rejected) inversion the model cannot single out "
            f"{model.candidate_state!r}" if non_unique else ""),
        "rule_compatible_states_note": non_inversion_note,
        "recorded_assumptions": list(model.recorded_assumptions),
    }

    record = DiagnosticRecordV1(
        schema_version=SCHEMA_VERSION,
        source=source,
        scope=model.scope,
        target_state_path=model.target_state_path,
        candidate_state=model.candidate_state,
        propositions=tuple(diagnoses),
        alternative_explanations=tuple(alternative_explanations),
        dependency_relations=tuple(dependency_relations),
        open_items=tuple(open_items),
        constraints=ConstraintsV1(),
        conclusion=conclusion,
        assumption_only_model=assumption_only,
        rejections=tuple(rejections),
        invalidated_items=tuple(invalidated),
        node_verdict_unchanged=model.node_verdict)
    digest = evidence_content_digest(json.dumps(
        _record_payload(record), ensure_ascii=False, sort_keys=True))
    return DiagnosticRecordV1(
        **{**record.__dict__, "record_digest": digest})


def _rejection_detail(code: str, item: EvidenceItemV1,
                      model: CandidateModelV1) -> str:
    if code == INVERSION_REJECTED:
        return ("forward whitelist recorded as rule_compatible_states "
                "was presented as a necessity basis — the whitelist "
                "defines rule applicability and proves neither set "
                "membership nor WHICH member")
    if code == SCOPE_MISMATCH_REJECTED:
        scope = item.scope
        return (f"evidence scope ({scope.paper_id} / "
                f"{scope.experimental_group_id} / stage {scope.stage} / "
                f"invocation {scope.invocation}) differs from the scope "
                f"under diagnosis ({model.scope.paper_id} / "
                f"{model.scope.experimental_group_id} / stage "
                f"{model.scope.stage} / invocation "
                f"{model.scope.invocation}) — cross-paper/cross-group "
                "evidence is rejected; paper/group must stay explicit "
                "(a same-group stage disagreement is "
                "stage_mismatch_rejected)" if scope else
                "evidence item fails the scope check")
    if code == STAGE_MISMATCH_REJECTED:
        scope = item.scope
        return (f"evidence scope stage {scope.stage!r} differs from the "
                f"stage under diagnosis {model.scope.stage!r} within the "
                f"SAME paper/group ({scope.paper_id} / "
                f"{scope.experimental_group_id}) — same-group "
                "cross-stage evidence is rejected, never silently "
                "re-staged (a cross-paper/cross-group disagreement is "
                "scope_mismatch_rejected)" if scope else "stage mismatch")
    if code == EVIDENCE_IDENTITY_MISSING_REJECTED:
        missing = ", ".join(_identity_missing_fields(item))
        return (f"a direct_evidence submission must carry a checkable "
                f"identity — non-empty content, a real source identity "
                f"(paper_explicit / supplement_explicit / "
                f"external_primary), its own scope (paper / group / "
                f"stage), and a provenance — missing: {missing}; the "
                f"item is rejected before any other check and never "
                f"enters the qualifying set (rule whitelists, proposal "
                f"assertions, and assumptions carry no source identity "
                f"by design and are exempt)")
    if code == INVOCATION_SWAP_REJECTED:
        scope = item.scope
        return (f"evidence belongs to invocation {scope.invocation!r} "
                f"but the invocation under diagnosis is "
                f"{model.scope.invocation!r} — result inheritance across "
                "invocations is forbidden; recorded as a swap, NOT as "
                "evidence" if scope else "invocation swap")
    if code == INVOCATION_UNBOUND_REJECTED:
        return ("evidence does not bind any protocol invocation; an "
                "unbound invocation cannot support an invocation-bound "
                f"target ({model.scope.invocation}) — the item is "
                "rejected, never silently re-scoped onto the invocation "
                "under diagnosis")
    if code == BINDING_MISMATCH_REJECTED:
        return (f"evidence individuates instance "
                f"{item.material_instance_id!r}, not the instance under "
                f"proof {model.target_material_instance_id!r}")
    if code == CIRCULAR_DEPENDENCY_REJECTED:
        return (f"support item cites downstream state path "
                f"{item.state_path!r}, which transitively depends on "
                f"{model.target_state_path!r} — downstream state may "
                "never be assumed in order to prove the upstream")
    if code == STALE_SOURCE_INVALIDATED:
        return ("the live source content digest no longer matches the "
                "item's content digest — the item is invalidated and "
                "recomputed honestly, never cited stale")
    return "rejected"


def _evidence_payload(item: EvidenceItemV1) -> dict:
    return {
        "content": item.content,
        "content_digest": item.content_digest,
        "source_identity": item.source_identity,
        "inference_nature": item.inference_nature,
        "provenance": item.provenance,
        "subject": item.subject,
        "scope": (None if item.scope is None else {
            "paper_id": item.scope.paper_id,
            "experimental_group_id": item.scope.experimental_group_id,
            "stage": item.scope.stage,
            "invocation": item.scope.invocation,
        }),
        "material_instance_id": item.material_instance_id,
        "state_path": item.state_path,
        "presented_as_necessity_basis": item.presented_as_necessity_basis,
    }


def _rejection_payload(entry: RejectionV1) -> dict:
    return {
        "code": entry.code,
        "proposition": entry.proposition,
        "detail": entry.detail,
        "provenance": entry.provenance,
    }


def _record_payload(record: DiagnosticRecordV1) -> dict:
    return {
        "schema_version": record.schema_version,
        "source": {
            "paper_id": record.source.paper_id,
            "experimental_group_id": record.source.experimental_group_id,
            "source_digest": record.source.source_digest,
            "figure_snapshot_digest": record.source.figure_snapshot_digest,
        },
        "scope": {
            "paper_id": record.scope.paper_id,
            "experimental_group_id": record.scope.experimental_group_id,
            "stage": record.scope.stage,
            "invocation": record.scope.invocation,
        },
        "target_state_path": record.target_state_path,
        "candidate_state": record.candidate_state,
        "propositions": [{
            "proposition": d.proposition,
            "evidence": [_evidence_payload(i) for i in d.evidence],
            "assumptions": list(d.assumptions),
            "verdict": d.verdict,
            "asserted_verdict": d.asserted_verdict,
            "qualifying_evidence": [
                _evidence_payload(i) for i in d.qualifying_evidence],
        } for d in record.propositions],
        "alternative_explanations": list(record.alternative_explanations),
        "dependency_relations": [{
            "dependent": r.dependent,
            "depends_on": r.depends_on,
            "kind": r.kind,
            "note": r.note,
        } for r in record.dependency_relations],
        "open_items": [{
            "proposition": o.proposition,
            "needed_evidence": o.needed_evidence,
        } for o in record.open_items],
        "constraints": {
            "diagnostics_only": record.constraints.diagnostics_only,
            "feeds_verdict": record.constraints.feeds_verdict,
        },
        "conclusion": record.conclusion,
        "assumption_only_model": dict(record.assumption_only_model),
        "rejections": [_rejection_payload(r) for r in record.rejections],
        "invalidated_items": [
            _rejection_payload(r) for r in record.invalidated_items],
        "node_verdict_unchanged": record.node_verdict_unchanged,
    }


def record_to_dict(record: DiagnosticRecordV1) -> dict:
    """Deterministic JSON-ready rendering of one diagnostic record."""
    payload = _record_payload(record)
    payload["record_digest"] = record.record_digest
    return payload
