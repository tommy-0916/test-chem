"""Round 3E: operation-precondition diagnostic evaluator tests.

Synthetic fixtures carrying the counter-example matrix semantics of the
chartered feasibility study
(``docs/field_semantic_gate_3e_design_20261003.md``): real Control
(calibration anchor), whitelist inversion, two compatible states,
instance/branch swap, first/second invocation swap, cross-group/
cross-stage export, circular dependency, and source mutation — plus
record-schema completeness and the owner-locked fixed-constraints block.

Every fixture uses the real A01 quotes verbatim where the quote itself is
the point (the operation name, the collective collection statement, the
first-invocation naming line); the graph and scope carriers are
synthetic.  Nothing here mints a token, derives with the engine, or
touches ``chem_agent_contracts/``.
"""

from __future__ import annotations

from hashlib import sha256
import json
import unittest

from reaserch_agent.route_operation_precondition_diagnostic import (
    ASSUMPTION,
    BINDING_MISMATCH_REJECTED,
    CIRCULAR_DEPENDENCY_REJECTED,
    CONDITIONAL_CONSTRAINT,
    DIRECT_EVIDENCE,
    INSUFFICIENT,
    INVERSION_REJECTED,
    INVOCATION_SWAP_REJECTED,
    NON_UNIQUE,
    OVER_CLAIM_REJECTED,
    P1_NECESSARY_INPUT_CONDITION,
    P2_THIS_MATERIAL_FLOW,
    P3_MATERIAL_INSTANCE_BINDING,
    PAPER_EXPLICIT,
    PROPOSAL_ASSERTION,
    PROPOSITION_SUBJECTS,
    PROPOSITIONS,
    PROVEN,
    RULE_COMPATIBLE_STATES,
    SCHEMA_VERSION,
    SCOPE_MISMATCH_REJECTED,
    STALE_SOURCE_INVALIDATED,
    SUPPLEMENT_EXPLICIT,
    UNPROVEN,
    CandidateModelV1,
    ConstraintsV1,
    DependencyRelationV1,
    EvidenceItemV1,
    PropositionClaimV1,
    ScopeBindingV1,
    SourceRefV1,
    build_dependency_view,
    evaluate_candidate_model,
    evidence_content_digest,
    record_to_dict,
)

# Real A01 quotes (verbatim, U+2212 where the paper uses it).
OPERATION_NAME_QUOTE = ("Finally, after a second centrifugation−"
                        "redispersion protocol one time")
COLLECTIVE_QUOTE = ("all the samples are collected for further "
                    "characterization")
NAMING_QUOTE = ("The precipitates were labeled as LDH seeds, which were "
                "dispersed in 30")
DEFINITION_QUOTE = ("by a centrifugation−redispersion protocol using "
                    "deionized water three times, which was the first "
                    "centrifugation−redispersion protocol")
E10_SI_CAPTION = ("(f) Optical image of NiFe E10, obtained after etching "
                  "with 10 mL nitric acid, showing clear salt solution "
                  "without precipitates.")

MS7A_OUT = "material_graph[7].material_outputs[0].state"
MS7B_IN = "material_graph[8].material_inputs[0].state"
MS7B_OUT = "material_graph[8].material_outputs[0].state"
G9_IN = "material_graph[9].material_inputs[0].state"
G9_OUT = "material_graph[9].material_outputs[0].state"

CONTROL_GROUP = "Synthesis of the Pristine Ni3Fe LDHs (NiFe Control)."
ETCHING_GROUP = ("Synthesis of the LDHs by an Etching Method of NiFe; "
                 "Ey (y = 1−10).")
PAPER_ID = "doi_10_1021_acsami_3c11651"
SOURCE_DIGEST = "sha256_" + "ab" * 32


def _a01_graph() -> list[dict]:
    """Synthetic graph with the real ms7a/ms7b/graph[9] cascade shape."""
    return [
        {"macro_step_id": "ms6",
         "material_inputs": [],
         "material_outputs": [{"material_instance_id": "inst_ldh_aged"}]},
        {"macro_step_id": "ms7a",
         "material_inputs": [{
             "material_instance_id": "inst_ldh_aged",
             "parent_output_refs": [{
                 "macro_step_id": "ms6",
                 "material_instance_id": "inst_ldh_aged"}]}],
         "material_outputs": [
             {"material_instance_id": "inst_ldh_wet_2"}]},
        {"macro_step_id": "ms7b",
         "material_inputs": [{
             "material_instance_id": "inst_ldh_wet_2",
             "parent_output_refs": [{
                 "macro_step_id": "ms7a",
                 "material_instance_id": "inst_ldh_wet_2"}]}],
         "material_outputs": [
             {"material_instance_id": "inst_ldh_washed"}]},
        {"macro_step_id": "ms8",
         "material_inputs": [{
             "material_instance_id": "inst_ldh_washed",
             "parent_output_refs": [{
                 "macro_step_id": "ms7b",
                 "material_instance_id": "inst_ldh_washed"}]}],
         "material_outputs": [{"material_instance_id": "inst_samples"}]},
    ]


# The graph indices of the synthetic fixture differ from the real A01
# chain; the fixture state paths are recomputed from the graph itself.
FIXTURE_PATHS = {
    "ms7a.out": "material_graph[1].material_outputs[0].state",
    "ms7b.in": "material_graph[2].material_inputs[0].state",
    "ms7b.out": "material_graph[2].material_outputs[0].state",
    "g9.in": "material_graph[3].material_inputs[0].state",
    "g9.out": "material_graph[3].material_outputs[0].state",
}


def _control_scope(invocation: str = "second") -> ScopeBindingV1:
    return ScopeBindingV1(PAPER_ID, CONTROL_GROUP, "ms7a", invocation)


def _source() -> SourceRefV1:
    return SourceRefV1(PAPER_ID, CONTROL_GROUP, SOURCE_DIGEST)


def _whitelist_item(*, presented_as_necessity: bool = False) -> EvidenceItemV1:
    return EvidenceItemV1(
        content=('REDISPERSION_V1 1.1.0 allowed_input_states = '
                 '["retained_wet_solid", "washed_wet_solid"], '
                 'output_states = ["suspension"], '
                 'liquid_participation.required = true'),
        source_identity="",
        inference_nature=RULE_COMPATIBLE_STATES,
        provenance="chem_resources/chemistry_conventions/conventions.json "
                   "L72-95",
        subject="rule_applicability",
        presented_as_necessity_basis=presented_as_necessity)


def _honest_control_model() -> CandidateModelV1:
    """The honest Control diagnosis: three propositions, no over-claim."""
    scope = _control_scope()
    return CandidateModelV1(
        target_state_path=FIXTURE_PATHS["ms7a.out"],
        candidate_state="retained_wet_solid",
        scope=scope,
        target_material_instance_id="inst_ldh_wet_2",
        compatible_states=("retained_wet_solid", "washed_wet_solid"),
        recorded_assumptions=(
            "flow assumption: ms7b.in is assumed to come from ms7a.out "
            "(no explicit inter-segment material-flow statement)",
            "continuity assumption: the material instance is assumed to "
            "be the same LDH-seeds batch across the second protocol"),
        propositions=(
            PropositionClaimV1(
                P1_NECESSARY_INPUT_CONDITION,
                evidence=(
                    EvidenceItemV1(
                        content=OPERATION_NAME_QUOTE,
                        source_identity=PAPER_EXPLICIT,
                        inference_nature=DIRECT_EVIDENCE,
                        provenance="fact:f_g7a_op",
                        subject="operation_occurrence",
                        scope=scope),
                    _whitelist_item(),
                ),
                assumptions=(
                    "the paper names the operation only; no retained-phase "
                    "statement for the second centrifugation exists (SI = "
                    "D: the three targeted evidence classes were not "
                    "detected)",)),
            PropositionClaimV1(
                P2_THIS_MATERIAL_FLOW,
                evidence=(
                    EvidenceItemV1(
                        content=("material_graph[8].material_inputs[0]."
                                 "parent_output_refs = [{macro_step_id: "
                                 "ms7a, material_instance_id: "
                                 "inst_ldh_wet_2}]"),
                        source_identity="",
                        inference_nature=PROPOSAL_ASSERTION,
                        provenance="proposal:parent_output_refs",
                        subject="proposal_drawn_edge",
                        material_instance_id="inst_ldh_wet_2"),
                    EvidenceItemV1(
                        content=("protocol_reference nodes "
                                 "proof_node_1e3047294567ed7b6b467dde / "
                                 "proof_node_181d2b69af719f90c74cf2c5 "
                                 "inherit operation_sequence "
                                 "[centrifugation, redispersion] and "
                                 "liquid_medium deionized water"),
                        source_identity=PAPER_EXPLICIT,
                        inference_nature=DIRECT_EVIDENCE,
                        provenance="r11:protocol_reference_nodes",
                        subject="operation_sequence_order",
                        scope=scope),
                ),
                assumptions=("flow assumption: the edge the proposal drew "
                             "is taken as the material flow",)),
            PropositionClaimV1(
                P3_MATERIAL_INSTANCE_BINDING,
                evidence=(
                    EvidenceItemV1(
                        content=COLLECTIVE_QUOTE,
                        source_identity=PAPER_EXPLICIT,
                        inference_nature=DIRECT_EVIDENCE,
                        provenance="fact:f_g8_out0_state",
                        subject="collective_collection_statement",
                        scope=scope),
                ),
                assumptions=("continuity assumption: the collective "
                             "statement is taken to cover the instance "
                             "under proof",)),
        ))


def _evaluate(model: CandidateModelV1, **kwargs):
    kwargs.setdefault("dependency_view", build_dependency_view(_a01_graph()))
    kwargs.setdefault("source", _source())
    return evaluate_candidate_model(model, **kwargs)


class RealControlCalibrationTest(unittest.TestCase):
    """Matrix case 1: real Control -> insufficient (calibration anchor)."""

    def test_real_control_concludes_insufficient(self):
        record = _evaluate(_honest_control_model())
        self.assertEqual(record.conclusion, INSUFFICIENT)
        verdicts = {d.proposition: d.verdict for d in record.propositions}
        self.assertEqual(verdicts, {
            P1_NECESSARY_INPUT_CONDITION: UNPROVEN,
            P2_THIS_MATERIAL_FLOW: UNPROVEN,
            P3_MATERIAL_INSTANCE_BINDING: UNPROVEN,
        })
        # The honest model records no rejection: the whitelist enters
        # ONLY as rule_compatible_states.
        self.assertEqual(record.rejections, ())
        # The assumption-only ceiling is a conditional constraint, and
        # still non-unique (two compatible states).
        ceiling = record.assumption_only_model
        self.assertEqual(ceiling["ceiling"], CONDITIONAL_CONSTRAINT)
        self.assertTrue(ceiling["non_unique"])
        self.assertEqual(ceiling["compatible_states"],
                         ["retained_wet_solid", "washed_wet_solid"])
        # The evaluated node stays BLOCKED.
        self.assertEqual(record.node_verdict_unchanged, "BLOCKED")

    def test_whitelist_recorded_only_as_rule_compatible_states(self):
        record = _evaluate(_honest_control_model())
        p1 = next(d for d in record.propositions
                  if d.proposition == P1_NECESSARY_INPUT_CONDITION)
        whitelist = [i for i in p1.evidence
                     if i.inference_nature == RULE_COMPATIBLE_STATES]
        self.assertEqual(len(whitelist), 1)
        self.assertFalse(whitelist[0].presented_as_necessity_basis)
        self.assertNotIn(whitelist[0], p1.qualifying_evidence)
        self.assertIn("rule_applicability", whitelist[0].subject)
        # The operation NAME never qualifies as a necessity basis either.
        names = [i for i in p1.evidence if i.subject == "operation_occurrence"]
        self.assertEqual(len(names), 1)
        self.assertEqual(p1.qualifying_evidence, ())


class WhitelistInversionTest(unittest.TestCase):
    """Matrix case 2: inversion of the forward rule -> inversion_rejected."""

    def test_inversion_rejected_and_non_authoritative(self):
        model = CandidateModelV1(
            target_state_path=FIXTURE_PATHS["ms7a.out"],
            candidate_state="retained_wet_solid",
            scope=_control_scope(),
            target_material_instance_id="inst_ldh_wet_2",
            compatible_states=("retained_wet_solid", "washed_wet_solid"),
            propositions=(
                PropositionClaimV1(
                    P1_NECESSARY_INPUT_CONDITION,
                    evidence=(_whitelist_item(presented_as_necessity=True),),
                    asserted_verdict=PROVEN),
            ))
        record = _evaluate(model)
        codes = [r.code for r in record.rejections]
        self.assertIn(INVERSION_REJECTED, codes)
        p1 = next(d for d in record.propositions
                  if d.proposition == P1_NECESSARY_INPUT_CONDITION)
        self.assertEqual(p1.verdict, UNPROVEN)
        self.assertEqual(p1.evidence, ())  # rejected item is not evidence
        self.assertEqual(record.conclusion, INSUFFICIENT)
        detail = next(r.detail for r in record.rejections
                      if r.code == INVERSION_REJECTED)
        self.assertIn("proves neither set membership nor WHICH member",
                      detail)


class TwoCompatibleStatesTest(unittest.TestCase):
    """Matrix case 3: even under inversion the set stays non_unique."""

    def test_non_unique_candidate_set(self):
        model = CandidateModelV1(
            target_state_path=FIXTURE_PATHS["ms7a.out"],
            candidate_state="retained_wet_solid",
            scope=_control_scope(),
            target_material_instance_id="inst_ldh_wet_2",
            compatible_states=("retained_wet_solid", "washed_wet_solid"),
            propositions=(
                PropositionClaimV1(
                    P1_NECESSARY_INPUT_CONDITION,
                    evidence=(_whitelist_item(presented_as_necessity=True),),
                    asserted_verdict=PROVEN),))
        record = _evaluate(model)
        ceiling = record.assumption_only_model
        self.assertTrue(ceiling["non_unique"])
        self.assertEqual(sorted(ceiling["compatible_states"]),
                         ["retained_wet_solid", "washed_wet_solid"])
        self.assertIn("cannot single out", ceiling["non_unique_note"])
        self.assertIn("retained_wet_solid", ceiling["non_unique_note"])
        self.assertEqual(record.conclusion, INSUFFICIENT)
        self.assertEqual(record.node_verdict_unchanged, "BLOCKED")


class InstanceBranchSwapTest(unittest.TestCase):
    """Matrix case 4: a candidate bound to another instance is rejected."""

    def test_binding_mismatch_rejected(self):
        scope = _control_scope()
        model = CandidateModelV1(
            target_state_path=FIXTURE_PATHS["ms7a.out"],
            candidate_state="retained_wet_solid",
            scope=scope,
            target_material_instance_id="inst_ldh_wet_2",
            propositions=(
                PropositionClaimV1(
                    P3_MATERIAL_INSTANCE_BINDING,
                    evidence=(
                        EvidenceItemV1(
                            content=("the aged LDH seeds suspension was "
                                     "divided into 8 parts"),
                            source_identity=PAPER_EXPLICIT,
                            inference_nature=DIRECT_EVIDENCE,
                            provenance="fact:other_instance",
                            subject="material_instance_identity",
                            scope=scope,
                            material_instance_id="inst_ldh_aged"),
                    ),
                    asserted_verdict=PROVEN),))
        record = _evaluate(model)
        codes = [r.code for r in record.rejections]
        self.assertIn(BINDING_MISMATCH_REJECTED, codes)
        p3 = next(d for d in record.propositions
                  if d.proposition == P3_MATERIAL_INSTANCE_BINDING)
        self.assertEqual(p3.verdict, UNPROVEN)
        detail = next(r.detail for r in record.rejections
                      if r.code == BINDING_MISMATCH_REJECTED)
        self.assertIn("inst_ldh_aged", detail)
        self.assertIn("inst_ldh_wet_2", detail)


class InvocationSwapTest(unittest.TestCase):
    """Matrix case 5: first-invocation evidence on the second invocation."""

    def test_invocation_swap_rejected(self):
        model = CandidateModelV1(
            target_state_path=FIXTURE_PATHS["ms7a.out"],
            candidate_state="retained_wet_solid",
            scope=_control_scope("second"),
            target_material_instance_id="inst_ldh_wet_2",
            propositions=(
                PropositionClaimV1(
                    P3_MATERIAL_INSTANCE_BINDING,
                    evidence=(
                        EvidenceItemV1(
                            content=NAMING_QUOTE,
                            source_identity=PAPER_EXPLICIT,
                            inference_nature=DIRECT_EVIDENCE,
                            provenance="fact:f_g5_out0_name",
                            subject="material_instance_identity",
                            scope=ScopeBindingV1(
                                PAPER_ID, CONTROL_GROUP, "ms5", "first"),
                            material_instance_id="inst_ldh_wet_2"),
                    ),
                    asserted_verdict=PROVEN),))
        record = _evaluate(model)
        codes = [r.code for r in record.rejections]
        self.assertIn(INVOCATION_SWAP_REJECTED, codes)
        p3 = next(d for d in record.propositions
                  if d.proposition == P3_MATERIAL_INSTANCE_BINDING)
        self.assertEqual(p3.verdict, UNPROVEN)
        self.assertEqual(p3.evidence, ())  # recorded as swap, NOT evidence
        detail = next(r.detail for r in record.rejections
                      if r.code == INVOCATION_SWAP_REJECTED)
        self.assertIn("first", detail)
        self.assertIn("second", detail)
        self.assertIn("NOT as evidence", detail)


class CrossGroupScopeTest(unittest.TestCase):
    """Matrix case 6: Control's conditional constraint must not export
    into the Etching group's scope (E10 counter-example semantics)."""

    def test_scope_mismatch_rejected(self):
        etching_item = EvidenceItemV1(
            content=E10_SI_CAPTION,
            source_identity=SUPPLEMENT_EXPLICIT,
            inference_nature=DIRECT_EVIDENCE,
            provenance="si-search:SI S5 Figure S1 caption",
            subject="post_wash_state",
            scope=ScopeBindingV1(PAPER_ID, ETCHING_GROUP, "etching_wash",
                                 "second"))
        model = CandidateModelV1(
            target_state_path=FIXTURE_PATHS["ms7a.out"],
            candidate_state="retained_wet_solid",
            scope=_control_scope(),
            target_material_instance_id="inst_ldh_wet_2",
            propositions=(
                PropositionClaimV1(
                    P1_NECESSARY_INPUT_CONDITION,
                    evidence=(etching_item,),
                    asserted_verdict=PROVEN),))
        record = _evaluate(model)
        codes = [r.code for r in record.rejections]
        self.assertIn(SCOPE_MISMATCH_REJECTED, codes)
        detail = next(r.detail for r in record.rejections
                      if r.code == SCOPE_MISMATCH_REJECTED)
        self.assertIn(ETCHING_GROUP, detail)
        self.assertIn(CONTROL_GROUP, detail)
        p1 = next(d for d in record.propositions
                  if d.proposition == P1_NECESSARY_INPUT_CONDITION)
        self.assertEqual(p1.verdict, UNPROVEN)
        # The scope under diagnosis keeps group/stage/invocation explicit.
        self.assertEqual(record.scope.experimental_group_id, CONTROL_GROUP)
        self.assertEqual(record.scope.stage, "ms7a")
        self.assertEqual(record.scope.invocation, "second")

    def test_conditional_constraint_not_exported(self):
        """The reverse direction: a Control-scoped item cited inside an
        Etching-scope diagnosis is rejected the same way."""
        control_item = EvidenceItemV1(
            content=OPERATION_NAME_QUOTE,
            source_identity=PAPER_EXPLICIT,
            inference_nature=DIRECT_EVIDENCE,
            provenance="fact:f_g7a_op",
            subject="operation_occurrence",
            scope=_control_scope())
        model = CandidateModelV1(
            target_state_path="etching.material_outputs[0].state",
            candidate_state="no_precipitate",
            scope=ScopeBindingV1(PAPER_ID, ETCHING_GROUP, "etching_wash",
                                 "second"),
            target_material_instance_id="inst_e10",
            propositions=(
                PropositionClaimV1(
                    P1_NECESSARY_INPUT_CONDITION,
                    evidence=(control_item,)),))
        record = evaluate_candidate_model(
            model,
            dependency_view=build_dependency_view([]),
            source=SourceRefV1(PAPER_ID, ETCHING_GROUP, SOURCE_DIGEST))
        codes = [r.code for r in record.rejections]
        self.assertIn(SCOPE_MISMATCH_REJECTED, codes)


class CircularDependencyTest(unittest.TestCase):
    """Matrix case 7: support citing downstream state is rejected."""

    def test_circular_dependency_rejected(self):
        scope = _control_scope()
        downstream_item = EvidenceItemV1(
            content="ms7b.in state is retained_wet_solid",
            source_identity="",
            inference_nature=ASSUMPTION,
            provenance="synthetic:ms7b.in",
            subject="inter_segment_material_flow",
            scope=scope,
            state_path=FIXTURE_PATHS["ms7b.in"])
        model = CandidateModelV1(
            target_state_path=FIXTURE_PATHS["ms7a.out"],
            candidate_state="retained_wet_solid",
            scope=scope,
            target_material_instance_id="inst_ldh_wet_2",
            propositions=(
                PropositionClaimV1(
                    P2_THIS_MATERIAL_FLOW, evidence=(downstream_item,)),))
        record = _evaluate(model)
        codes = [r.code for r in record.rejections]
        self.assertIn(CIRCULAR_DEPENDENCY_REJECTED, codes)
        detail = next(r.detail for r in record.rejections
                      if r.code == CIRCULAR_DEPENDENCY_REJECTED)
        self.assertIn(FIXTURE_PATHS["ms7b.in"], detail)
        self.assertIn("transitively depends on", detail)

    def test_evaluator_builds_its_own_dependency_view(self):
        view = build_dependency_view(_a01_graph())
        closure = view.downstream_closure(FIXTURE_PATHS["ms7a.out"])
        self.assertEqual(set(closure), {
            FIXTURE_PATHS["ms7b.in"], FIXTURE_PATHS["ms7b.out"],
            FIXTURE_PATHS["g9.in"], FIXTURE_PATHS["g9.out"]})
        # The cascade relation the record must carry.
        self.assertIn(FIXTURE_PATHS["ms7a.out"],
                      view.parents_of(FIXTURE_PATHS["ms7b.in"]))

    def test_circular_guard_rejects_transitive_downstream(self):
        """Citing graph[9].out (two hops downstream) is still circular."""
        scope = _control_scope()
        item = EvidenceItemV1(
            content="the collected samples are suspension",
            source_identity=PAPER_EXPLICIT,
            inference_nature=DIRECT_EVIDENCE,
            provenance="synthetic:g9.out",
            subject="inter_segment_material_flow",
            scope=scope,
            state_path=FIXTURE_PATHS["g9.out"])
        model = CandidateModelV1(
            target_state_path=FIXTURE_PATHS["ms7a.out"],
            candidate_state="retained_wet_solid",
            scope=scope,
            target_material_instance_id="inst_ldh_wet_2",
            propositions=(
                PropositionClaimV1(
                    P2_THIS_MATERIAL_FLOW, evidence=(item,)),))
        record = _evaluate(model)
        codes = [r.code for r in record.rejections]
        self.assertIn(CIRCULAR_DEPENDENCY_REJECTED, codes)


class SourceMutationTest(unittest.TestCase):
    """Matrix case 8: mutating the naming evidence invalidates honestly."""

    def _first_invocation_model(self, naming_item: EvidenceItemV1):
        return CandidateModelV1(
            target_state_path="material_graph[0].material_outputs[0].state",
            candidate_state="retained_wet_solid",
            scope=ScopeBindingV1(PAPER_ID, CONTROL_GROUP, "ms5", "first"),
            target_material_instance_id="inst_ldh_seeds",
            propositions=(
                PropositionClaimV1(
                    P3_MATERIAL_INSTANCE_BINDING,
                    evidence=(naming_item,),),))

    def test_source_mutation_invalidates_and_recomputes(self):
        naming = EvidenceItemV1(
            content=NAMING_QUOTE,
            source_identity=PAPER_EXPLICIT,
            inference_nature=DIRECT_EVIDENCE,
            provenance="fact:f_g5_out0_name",
            subject="material_instance_identity",
            scope=ScopeBindingV1(PAPER_ID, CONTROL_GROUP, "ms5", "first"),
            material_instance_id="inst_ldh_seeds")
        live = {"fact:f_g5_out0_name": NAMING_QUOTE}
        record = evaluate_candidate_model(
            self._first_invocation_model(naming),
            dependency_view=build_dependency_view([]),
            source=_source(), live_sources=live)
        p3 = next(d for d in record.propositions
                  if d.proposition == P3_MATERIAL_INSTANCE_BINDING)
        self.assertEqual(p3.verdict, PROVEN)  # individuating, in-scope
        self.assertEqual(record.invalidated_items, ())

        # Mutate the source: every item citing it invalidates honestly.
        mutated = "The precipitates were labeled as NiFe hydroxide seeds"
        live_mutated = {"fact:f_g5_out0_name": mutated}
        record2 = evaluate_candidate_model(
            self._first_invocation_model(naming),
            dependency_view=build_dependency_view([]),
            source=_source(), live_sources=live_mutated)
        codes = [r.code for r in record2.rejections]
        self.assertIn(STALE_SOURCE_INVALIDATED, codes)
        p3b = next(d for d in record2.propositions
                   if d.proposition == P3_MATERIAL_INSTANCE_BINDING)
        self.assertEqual(p3b.verdict, UNPROVEN)
        self.assertEqual(p3b.evidence, ())  # no stale citation survives
        self.assertEqual(len(record2.invalidated_items), 1)
        self.assertEqual(record2.invalidated_items[0].provenance,
                         "fact:f_g5_out0_name")
        self.assertEqual(record2.conclusion, INSUFFICIENT)

        # Honest recompute: a NEW item quoting the mutated content
        # validates under the mutated source — digests move, nothing
        # stale is cited.
        naming_mutated = EvidenceItemV1(
            content=mutated,
            source_identity=PAPER_EXPLICIT,
            inference_nature=DIRECT_EVIDENCE,
            provenance="fact:f_g5_out0_name",
            subject="material_instance_identity",
            scope=ScopeBindingV1(PAPER_ID, CONTROL_GROUP, "ms5", "first"),
            material_instance_id="inst_ldh_seeds")
        record3 = evaluate_candidate_model(
            self._first_invocation_model(naming_mutated),
            dependency_view=build_dependency_view([]),
            source=_source(), live_sources=live_mutated)
        self.assertEqual(record3.invalidated_items, ())
        p3c = next(d for d in record3.propositions
                   if d.proposition == P3_MATERIAL_INSTANCE_BINDING)
        self.assertEqual(p3c.verdict, PROVEN)
        self.assertEqual(p3c.evidence[0].content_digest,
                         evidence_content_digest(mutated))
        self.assertNotEqual(p3c.evidence[0].content_digest,
                            naming.content_digest)
        # The untouched record still verifies against the untouched live
        # source (recompute changed nothing retroactively).
        record4 = evaluate_candidate_model(
            self._first_invocation_model(naming),
            dependency_view=build_dependency_view([]),
            source=_source(), live_sources=live)
        self.assertEqual(record_to_dict(record4), record_to_dict(record))


class RecordSchemaTest(unittest.TestCase):
    """The charter paper-v1 record: 7 fields + fixed constraints."""

    def test_record_schema_completeness(self):
        record = _evaluate(
            _honest_control_model(),
            alternative_explanations=(
                "the supernatant was retained instead (the text does "
                "not constrain which phase was kept)",
                "aliquot/portion flow among the 8 divided parts",
            ),
            dependency_relations=(
                DependencyRelationV1(
                    dependent=FIXTURE_PATHS["ms7b.in"],
                    depends_on=FIXTURE_PATHS["ms7a.out"],
                    kind="dependency_cascade",
                    note="ms7b.in <- ms7a.out"),))
        payload = record_to_dict(record)
        # (1) source + figure-snapshot digest
        self.assertEqual(set(payload["source"]), {
            "paper_id", "experimental_group_id", "source_digest",
            "figure_snapshot_digest"})
        # (2) experimental group / stage / invocation
        self.assertEqual(set(payload["scope"]), {
            "paper_id", "experimental_group_id", "stage", "invocation"})
        # (3) three propositions, separately
        self.assertEqual([p["proposition"] for p in payload["propositions"]],
                         list(PROPOSITIONS))
        for proposition in payload["propositions"]:
            self.assertEqual(set(proposition), {
                "proposition", "evidence", "assumptions", "verdict",
                "asserted_verdict", "qualifying_evidence"})
            self.assertIn(proposition["verdict"], (PROVEN, UNPROVEN))
            for item in proposition["evidence"]:
                self.assertEqual(set(item), {
                    "content", "content_digest", "source_identity",
                    "inference_nature", "provenance", "subject", "scope",
                    "material_instance_id", "state_path",
                    "presented_as_necessity_basis"})
        # (4) alternative explanations
        self.assertEqual(len(payload["alternative_explanations"]), 2)
        # (5) dependency relations
        self.assertEqual(payload["dependency_relations"][0]["kind"],
                         "dependency_cascade")
        # (6) open items: one per unproven proposition
        self.assertEqual({o["proposition"] for o in payload["open_items"]},
                         set(PROPOSITIONS))
        # (7) constraints block
        self.assertEqual(payload["constraints"], {
            "diagnostics_only": True, "feeds_verdict": False})
        # Schema identity + conclusion + unchanged verdict.
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["conclusion"], INSUFFICIENT)
        self.assertEqual(payload["node_verdict_unchanged"], "BLOCKED")
        self.assertTrue(payload["record_digest"].startswith("sha256_"))

    def test_fixed_constraints_block_is_enforced(self):
        with self.assertRaises(ValueError):
            ConstraintsV1(diagnostics_only=False)
        with self.assertRaises(ValueError):
            ConstraintsV1(feeds_verdict=True)
        record = _evaluate(_honest_control_model())
        self.assertTrue(record.constraints.diagnostics_only)
        self.assertFalse(record.constraints.feeds_verdict)

    def test_source_identity_separate_from_inference_nature(self):
        item = _whitelist_item()
        self.assertEqual(item.source_identity, "")
        self.assertEqual(item.inference_nature, RULE_COMPATIBLE_STATES)
        paper_item = EvidenceItemV1(
            content=OPERATION_NAME_QUOTE,
            source_identity=PAPER_EXPLICIT,
            inference_nature=DIRECT_EVIDENCE,
            subject="operation_occurrence")
        self.assertEqual(paper_item.source_identity, PAPER_EXPLICIT)
        self.assertEqual(paper_item.inference_nature, DIRECT_EVIDENCE)

    def test_over_claim_flagged_without_specific_code(self):
        model = CandidateModelV1(
            target_state_path=FIXTURE_PATHS["ms7a.out"],
            candidate_state="retained_wet_solid",
            scope=_control_scope(),
            target_material_instance_id="inst_ldh_wet_2",
            propositions=(
                PropositionClaimV1(
                    P1_NECESSARY_INPUT_CONDITION,
                    evidence=(
                        EvidenceItemV1(
                            content="an irrelevant paper sentence",
                            source_identity=PAPER_EXPLICIT,
                            inference_nature=DIRECT_EVIDENCE,
                            subject="operation_occurrence",
                            scope=_control_scope()),),
                    asserted_verdict=PROVEN),))
        record = _evaluate(model)
        codes = [r.code for r in record.rejections]
        self.assertIn(OVER_CLAIM_REJECTED, codes)

    def test_proposition_subjects_are_charter_fixed(self):
        self.assertEqual(PROPOSITION_SUBJECTS, {
            P1_NECESSARY_INPUT_CONDITION: "necessary_input_condition",
            P2_THIS_MATERIAL_FLOW: "inter_segment_material_flow",
            P3_MATERIAL_INSTANCE_BINDING: "material_instance_identity",
        })

    def test_collective_statement_never_individuates(self):
        """'all the samples' is paper_explicit but collective: P3 stays
        unproven and the continuity assumption is recorded."""
        record = _evaluate(_honest_control_model())
        p3 = next(d for d in record.propositions
                  if d.proposition == P3_MATERIAL_INSTANCE_BINDING)
        self.assertEqual(p3.verdict, UNPROVEN)
        self.assertEqual(len(p3.evidence), 1)
        self.assertEqual(p3.evidence[0].source_identity, PAPER_EXPLICIT)
        self.assertEqual(p3.evidence[0].material_instance_id, "")
        self.assertTrue(any("continuity" in a for a in p3.assumptions))

    def test_evaluator_is_deterministic(self):
        a = record_to_dict(_evaluate(_honest_control_model()))
        b = record_to_dict(_evaluate(_honest_control_model()))
        self.assertEqual(json.dumps(a, sort_keys=True),
                         json.dumps(b, sort_keys=True))
        self.assertEqual(a["record_digest"], b["record_digest"])
        self.assertEqual(a["record_digest"],
                         "sha256_" + sha256(json.dumps(
                             {k: v for k, v in a.items()
                              if k != "record_digest"},
                             ensure_ascii=False,
                             sort_keys=True).encode("utf-8")).hexdigest())

    def test_open_items_name_the_closing_evidence(self):
        record = _evaluate(_honest_control_model())
        open_items = {o.proposition: o.needed_evidence
                      for o in record.open_items}
        self.assertIn("retained phase",
                      open_items[P1_NECESSARY_INPUT_CONDITION])
        self.assertIn("inter-segment material-flow",
                      open_items[P2_THIS_MATERIAL_FLOW])
        self.assertIn("instance-individuating",
                      open_items[P3_MATERIAL_INSTANCE_BINDING])


if __name__ == "__main__":
    unittest.main()
