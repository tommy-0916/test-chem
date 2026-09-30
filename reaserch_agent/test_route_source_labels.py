"""Unit tests for scoped source-label associations (semantic gate slice)."""

from __future__ import annotations

import unittest

from chem_agent_contracts.route_source_labels import (
    RULE_BARE_STATE_MENTION,
    RULE_DEFINITION_SITE_CONCENTRATION,
    RULE_EMBEDDED_STATE_LABEL,
    RULE_SCOPED_LABEL_IDENTITY,
    SOURCE_LABEL_RULE_VERSION,
    attributed_state_mention,
    build_source_label_context,
    competing_quantity_identity_surfaces,
    definition_site_concentration_binding,
    quantity_identity_surfaces,
)
from reaserch_agent.route_group_compiler import (
    _fact_issue,
    material_id_graph_issue,
    quantity_has_local_attribution,
)

PH_SENTENCE = (
    "The pH of the solution was monitored using a pH meter (Mettler Toledo) "
    "and controlled to be 10 by dropwise adding solution B manually."
)
INJECT_SENTENCE = (
    "Then, solution A is injected into solution C in a beaker using a syringe "
    "pump at a flow speed of 115 mL/h, and the reaction was kept at a "
    "stirring speed of 800 rpm."
)
DEF_B_SENTENCE = (
    "Solution B is prepared by dissolving 100 mmol NaOH in 100 mL of water "
    "(1 M)."
)


def _fact(fact_id, path, value, excerpt, unit=""):
    return {
        "fact_id": fact_id,
        "field_path": path,
        "value": value,
        "unit": unit,
        "excerpt": excerpt,
        "required": True,
    }


def _port(step, kind, index, material_id, name, state="solution", sample="s1"):
    return (
        f"material_graph[{step}].material_{kind}[{index}]",
        {"material_id": material_id, "name": name, "state": state},
    )


def _graph(*ports, operation="mixed", sample="s1"):
    graph = [{
        "macro_step_id": "step_1", "sequence": 1, "operation": operation,
        "sample_id": sample,
        "material_inputs": [], "material_intermediates": [],
        "material_outputs": [],
    }]
    for path, port in ports:
        kind = path.rsplit("material_", 1)[1].split("[")[0]
        graph[0]["material_" + kind].append(port)
    return graph


def _nife_facts():
    facts = [
        _fact("f_a_name", "material_graph[0].material_inputs[0].name",
              "solution A", INJECT_SENTENCE),
        _fact("f_b_name", "material_graph[0].material_inputs[1].name",
              "solution B", PH_SENTENCE),
        _fact("f_b_conc", "material_graph[0].material_inputs[1].concentration_value",
              1, DEF_B_SENTENCE, unit="M"),
        _fact("f_mix_name_a", "material_graph[0].material_inputs[2].name",
              "reaction", INJECT_SENTENCE),
        _fact("f_mix_name_b", "material_graph[0].material_outputs[0].name",
              "solution", PH_SENTENCE),
    ]
    graph = _graph(
        _port(0, "inputs", 0, "mat_solution_a", "solution A"),
        _port(0, "inputs", 1, "mat_solution_b", "solution B"),
        _port(0, "inputs", 2, "mat_reaction_mixture", "reaction"),
        _port(0, "outputs", 0, "mat_reaction_mixture", "solution"),
    )
    graph[0]["material_inputs"][1]["concentration_value"] = 1
    graph[0]["material_inputs"][1]["concentration_unit"] = "M"
    return graph, facts


class SourceLabelContextTests(unittest.TestCase):
    def test_labels_collect_surfaces_without_cross_entity_merge(self):
        graph, facts = _nife_facts()
        context = build_source_label_context(graph, facts)
        solution_b = context.by_material["mat_solution_b"]
        self.assertEqual(solution_b.labels, frozenset({"solution b"}))
        self.assertEqual(
            solution_b.surfaces["solution b"],
            frozenset({"solution B", "Solution B"}),
        )
        self.assertEqual(
            context.by_material["mat_solution_a"].labels,
            frozenset({"solution a"}),
        )
        self.assertNotIn("solution a", solution_b.labels)

    def test_bare_state_owner_requires_group_unique_label(self):
        graph, facts = _nife_facts()
        context = build_source_label_context(graph, facts)
        self.assertEqual(
            context.bare_state_owner["solution"], "mat_reaction_mixture",
        )
        self.assertNotIn("solution a", context.bare_state_owner)

    def test_shared_bare_label_drops_ownership(self):
        graph, facts = _nife_facts()
        facts.append(_fact(
            "f_other_name", "material_graph[0].material_outputs[1].name",
            "solution", PH_SENTENCE,
        ))
        graph[0]["material_outputs"].append({
            "material_id": "mat_other", "name": "solution", "state": "solution",
        })
        context = build_source_label_context(graph, facts)
        self.assertNotIn("solution", context.bare_state_owner)


class StateMentionAttributionTests(unittest.TestCase):
    def test_name_embedded_state_resolves_amid_repeated_mentions(self):
        graph, facts = _nife_facts()
        context = build_source_label_context(graph, facts)
        binding = attributed_state_mention(
            "solution", PH_SENTENCE,
            "material_graph[0].material_inputs[1].state", graph, context,
        )
        self.assertIsNotNone(binding)
        self.assertEqual(binding["rule_id"], RULE_EMBEDDED_STATE_LABEL)
        self.assertEqual(binding["source_surface"], "solution B")
        self.assertEqual(binding["entity_material_id"], "mat_solution_b")

    def test_bare_state_mention_resolves_to_unique_owner(self):
        graph, facts = _nife_facts()
        context = build_source_label_context(graph, facts)
        binding = attributed_state_mention(
            "solution", PH_SENTENCE,
            "material_graph[0].material_outputs[0].state", graph, context,
        )
        self.assertIsNotNone(binding)
        self.assertEqual(binding["rule_id"], RULE_BARE_STATE_MENTION)
        self.assertEqual(binding["entity_material_id"], "mat_reaction_mixture")

    def test_injection_sentence_binds_named_solution_by_embedded_label(self):
        graph, facts = _nife_facts()
        context = build_source_label_context(graph, facts)
        binding = attributed_state_mention(
            "solution", INJECT_SENTENCE,
            "material_graph[0].material_inputs[0].state", graph, context,
        )
        self.assertIsNotNone(binding)
        self.assertEqual(binding["source_surface"], "solution A")

    def test_state_word_only_inside_foreign_label_stays_unresolved(self):
        excerpt = "Then, solution B was added to the vial."
        graph, facts = _nife_facts()
        context = build_source_label_context(graph, facts)
        binding = attributed_state_mention(
            "solution", excerpt,
            "material_graph[0].material_outputs[0].state", graph, context,
        )
        self.assertIsNone(binding)

    def test_two_bare_mentions_of_one_entity_stay_unresolved(self):
        excerpt = "The solution was added to the solution slowly."
        graph, facts = _nife_facts()
        context = build_source_label_context(graph, facts)
        binding = attributed_state_mention(
            "solution", excerpt,
            "material_graph[0].material_outputs[0].state", graph, context,
        )
        self.assertIsNone(binding)


class MaterialIdIdentityTests(unittest.TestCase):
    def _step(self, names, *, step_id="m1", sequence=1, sample="s1"):
        inputs, outputs = names
        step = {
            "macro_step_id": step_id, "sequence": sequence,
            "operation": "aged", "sample_id": sample,
            "material_inputs": [], "material_intermediates": [],
            "material_outputs": [],
        }
        for index, name in enumerate(inputs):
            if not name:
                continue
            step["material_inputs"].append({
                "material_id": "mat_x",
                "material_instance_id": f"i_in_{step_id}_{index}",
                "name": name, "state": "solution",
            })
        for index, name in enumerate(outputs):
            if not name:
                continue
            step["material_outputs"].append({
                "material_id": "mat_x",
                "material_instance_id": f"i_out_{step_id}_{index}",
                "name": name, "state": "solution",
            })
        return step

    def test_bare_stage_labels_need_continuity_evidence(self):
        # One step in isolation: two bare names, no shared instance, no
        # chain — empty cores alone must not prove one batch.
        lonely = self._step((("reaction",), ("solution",)), sequence=3)
        self.assertEqual(
            material_id_graph_issue([lonely]),
            "route_group_material_id_identity_conflict",
        )

    def test_bare_stage_labels_chained_across_steps_pass(self):
        # reaction -> solution -> suspension carried by the same ID across
        # neighbouring steps, as in the NiFe control proposal.
        graph = [
            self._step(((), ("reaction",)), step_id="m1", sequence=1),
            self._step((("reaction",), ("solution",)), step_id="m2", sequence=2),
            self._step((("solution",), ("suspension",)), step_id="m3", sequence=3),
            self._step((("suspension",), ()), step_id="m4", sequence=4),
        ]
        self.assertEqual(material_id_graph_issue(graph), "")

    def test_bare_stage_labels_with_shared_instance_pass(self):
        step = self._step((("reaction",), ("solution",)), sequence=1)
        step["material_inputs"][0]["material_instance_id"] = "i_batch"
        step["material_outputs"][0]["material_instance_id"] = "i_batch"
        self.assertEqual(material_id_graph_issue([step]), "")

    def test_distinct_cores_still_conflict(self):
        step = self._step((("Solution A",), ("Solution B",)), sequence=1)
        self.assertEqual(
            material_id_graph_issue([step]),
            "route_group_material_id_identity_conflict",
        )

    def test_same_core_aliases_pass(self):
        graph = [
            self._step(((), ("NiFe solution",)), step_id="m1", sequence=1),
            self._step((("NiFe solution",), ("NiFe suspension",)),
                       step_id="m2", sequence=2),
            self._step((("NiFe suspension",), ()), step_id="m3", sequence=3),
        ]
        self.assertEqual(material_id_graph_issue(graph), "")

    def test_terminal_rename_without_continuity_conflicts(self):
        graph = [
            self._step(((), ("NiFe solution",)), step_id="m1", sequence=1),
            self._step((("NiFe solution",), ("NiFe suspension",)),
                       step_id="m2", sequence=2),
        ]
        self.assertEqual(
            material_id_graph_issue(graph),
            "route_group_material_id_identity_conflict",
        )


class ConcentrationAttributionTests(unittest.TestCase):
    def _context(self):
        graph, facts = _nife_facts()
        return build_source_label_context(graph, facts)

    def test_scoped_label_surfaces_match_case_variant(self):
        graph, _facts = _nife_facts()
        surfaces = quantity_identity_surfaces(
            "material_graph[0].material_inputs[1].concentration_value",
            graph, self._context(),
        )
        self.assertEqual(surfaces, ("Solution B", "solution B"))

    def test_definition_site_concentration_binding(self):
        binding = definition_site_concentration_binding(
            DEF_B_SENTENCE, 1, "M", ("Solution B", "solution B"),
        )
        self.assertIsNotNone(binding)
        self.assertEqual(binding["rule_id"], RULE_DEFINITION_SITE_CONCENTRATION)
        self.assertEqual(binding["source_surface"], "Solution B")

    def test_definition_site_binds_renamed_quantified_preparations(self):
        examples = (
            ("Buffer Omega", "Buffer Omega was made by dissolving 0.2 g salt "
             "in 50 mL solvent (4 mM).", 4, "mM"),
            ("Mixture Q", "The Mixture Q is formed by dissolving 2 mmol salt "
             "and 1 mmol additive in 20 mL solvent (0.1 M).", 0.1, "M"),
            ("Feed Z", "An unrelated batch was cooled.\tFeed Z was prepared "
             "by dissolving 10 mg reagent in 5 mL carrier (2 mM).", 2, "mM"),
        )
        for label, excerpt, value, unit in examples:
            with self.subTest(label=label):
                binding = definition_site_concentration_binding(
                    excerpt, value, unit, (label,),
                )
                self.assertIsNotNone(binding)
                self.assertEqual(binding["source_surface"], label)

    def test_definition_site_reproduced_parenthetical_binds_despite_competing_entities(self):
        # 100 mmol in 100 mL reproduces the trailing (1 M): the readings
        # "the product is 1 M" and "NaOH in this product is 1 M" coincide.
        binding = definition_site_concentration_binding(
            DEF_B_SENTENCE, 1, "M", ("Solution B", "solution B"),
            competing_surfaces=("NaOH", "water", "solution"),
        )
        self.assertIsNotNone(binding)
        self.assertEqual(binding["source_surface"], "Solution B")

    def test_definition_site_competing_entities_stay_unresolved_without_reproduction(self):
        binding = definition_site_concentration_binding(
            DEF_B_SENTENCE, 2, "M", ("Solution B", "solution B"),
            competing_surfaces=("NaOH", "water", "solution"),
        )
        self.assertIsNone(binding)

    def test_definition_site_reproduction_requires_a_unique_quotient(self):
        sentence = ("Solution Q was prepared by dissolving 2 mmol salt and "
                    "2 mmol additive in 20 mL water (0.1 M).")
        binding = definition_site_concentration_binding(
            sentence, 0.1, "M", ("Solution Q",),
            competing_surfaces=("salt", "additive"),
        )
        self.assertIsNotNone(binding)
        self.assertEqual(binding["source_surface"], "Solution Q")
        unresolved = ("Solution Q was prepared by dissolving 2 mmol salt and "
                      "1 mmol additive in 20 mL water (0.1 M).")
        self.assertIsNone(definition_site_concentration_binding(
            unresolved, 0.1, "M", ("Solution Q",),
            competing_surfaces=("salt", "additive"),
        ))

    def test_definition_site_reproduction_stays_unresolved_for_nonwater_solvent(self):
        # 2 mmol in 2 mL happens to equal 1 M, yet the parenthetical could
        # just as well describe the carrier solution itself, so the competing
        # readings do not collapse.
        excerpt = ("Solution A was prepared by dissolving 2 mmol salt "
                   "in 2 mL Carrier Z (1 M).")
        self.assertIsNone(definition_site_concentration_binding(
            excerpt, 1, "M", ("Solution A",),
            competing_surfaces=("Carrier Z",),
        ))

    def test_competing_surfaces_exclude_same_entity_aliases(self):
        graph, facts = _nife_facts()
        context = build_source_label_context(graph, facts)
        other = competing_quantity_identity_surfaces(
            "material_graph[0].material_inputs[1].concentration_value",
            graph, context,
        )
        self.assertIn("solution A", other)
        self.assertIn("solution", other)
        self.assertNotIn("solution B", other)
        self.assertNotIn("Solution B", other)
        binding = definition_site_concentration_binding(
            DEF_B_SENTENCE, 1, "M", ("Solution B", "solution B"),
            competing_surfaces=other,
        )
        self.assertIsNotNone(binding)

    def test_definition_site_rejects_cross_subject_and_multi_event_prose(self):
        templates = (
            "{a} was washed, then {b} was prepared by dissolving 2 mmol salt "
            "in 20 mL water (1 M).",
            "{a} and {b} were prepared by dissolving 2 mmol salt in 20 mL water (1 M).",
            "{a} was prepared by dissolving 2 mmol salt in 20 mL water; "
            "{b} was prepared (1 M).",
            "{a} was prepared by dissolving 2 mmol salt in 20 mL water "
            "and was cooled (1 M).",
            "{a} was prepared by dissolving 2 mmol salt in 20 mL water "
            "and stirred for an hour (1 M).",
            "{a} was prepared by dissolving 2 mmol salt in 20 mL water "
            "before {b} was added (1 M).",
        )
        for a, b in (("Solution A", "Solution B"), ("Batch Violet", "Batch Teal")):
            for template in templates:
                excerpt = template.format(a=a, b=b)
                with self.subTest(excerpt=excerpt):
                    self.assertIsNone(definition_site_concentration_binding(
                        excerpt, 1, "M", (a,), competing_surfaces=(b,),
                    ))

    def test_definition_site_rejects_negation_condition_and_quotation(self):
        predicate = "dissolving 2 mmol reagent in 20 mL solvent (1 M)."
        examples = (
            "Feed Q was not prepared by " + predicate,
            "Feed Q was never prepared by " + predicate,
            "Feed Q might be prepared by " + predicate,
            "If Feed Q was prepared by " + predicate,
            "Feed Q was prepared by dissolving no reagent in 20 mL solvent (1 M).",
            "Feed Q was prepared by dissolving 2 mmol reagent "
            "in 20 mL solvent without heating (1 M).",
            'The report proposed "Feed Q was prepared by ' + predicate + '"',
        )
        for excerpt in examples:
            with self.subTest(excerpt=excerpt):
                self.assertIsNone(definition_site_concentration_binding(
                    excerpt, 1, "M", ("Feed Q",),
                ))

    def test_definition_site_does_not_borrow_stock_object_concentration(self):
        examples = (
            "Feed A was prepared by mixing Feed B (1 M).",
            "Feed A was prepared by mixing 5 mL Feed B with water (1 M).",
            "Feed A was prepared by diluting 5 mL Feed B in 20 mL water (1 M).",
            "Feed A was prepared by dissolving 2 mmol stock solution "
            "in 20 mL water (1 M).",
            "Feed A was prepared by dissolving 2 mmol reagent "
            "in 20 mL water and Feed B (1 M).",
            "Feed A was prepared by dissolving 2 mmol reagent "
            "in 20 mL Feed B (1 M).",
        )
        for excerpt in examples:
            with self.subTest(excerpt=excerpt):
                self.assertIsNone(definition_site_concentration_binding(
                    excerpt, 1, "M", ("Feed A",), competing_surfaces=("Feed B",),
                ))

    def test_definition_site_rejects_shared_subject_label(self):
        self.assertIsNone(definition_site_concentration_binding(
            DEF_B_SENTENCE, 1, "M", ("Solution B",),
            competing_surfaces=("Solution B",),
        ))

    def test_definition_site_rejects_non_concentration_unit(self):
        self.assertIsNone(definition_site_concentration_binding(
            "Solution B is prepared by dissolving NaOH in 100 mL of water.",
            100, "mL", ("Solution B",),
        ))

    def test_definition_site_rejects_surface_after_verb(self):
        excerpt = "NaOH in water was prepared as Solution B (1 M)."
        binding = definition_site_concentration_binding(
            excerpt, 1, "M", ("Solution B",),
        )
        self.assertIsNone(binding)

    def test_definition_site_rejects_unparenthesized_amount(self):
        excerpt = "Solution B is a 1 M NaOH solution."
        self.assertIsNone(definition_site_concentration_binding(
            excerpt, 1, "M", ("Solution B",),
        ))

    def test_definition_site_rejects_repeated_same_unit_amount(self):
        excerpt = ("Solution B (1 M) is prepared by diluting a 1 M stock "
                   "with water (1 M).")
        self.assertIsNone(definition_site_concentration_binding(
            excerpt, 1, "M", ("Solution B",),
        ))

    def test_scoped_label_does_not_merge_solution_a_with_solution_b(self):
        graph, _facts = _nife_facts()
        surfaces = quantity_identity_surfaces(
            "material_graph[0].material_inputs[1].concentration_value",
            graph, self._context(),
        )
        excerpt = "Solution A (1 M) was prepared separately."
        self.assertFalse(quantity_has_local_attribution(
            excerpt, 1, "M", identity="solution B", identity_required=True,
            identity_surfaces=surfaces,
        ))


class CompilerBindingRecordTests(unittest.TestCase):
    def _issue(self, fact, graph, facts, sink):
        source = {
            "paper_id": "paper-A", "experimental_group_id": "Group A",
            "section": "Methods", "locator": "lines:1-1",
            "source_digest": "sha256_" + "a" * 64,
        }
        fact = dict(fact, source=source)
        return _fact_issue(
            fact, paper_id="paper-A", group_id="Group A", section="Methods",
            document_digest="sha256_" + "a" * 64, graph=graph,
            signature={"route_family": "r", "target_transformation": "t",
                       "endpoint_state": "solution"},
            facts=facts, binding_sink=sink,
        )

    def test_state_fact_records_binding(self):
        graph, facts = _nife_facts()
        state_fact = _fact(
            "f_b_state", "material_graph[0].material_inputs[1].state",
            "solution", PH_SENTENCE,
        )
        sink: dict = {}
        issue = self._issue(state_fact, graph, facts + [state_fact], sink)
        self.assertEqual(issue, "")
        binding = sink["binding"]
        self.assertEqual(binding["rule_version"], SOURCE_LABEL_RULE_VERSION)
        self.assertEqual(binding["entity_material_id"], "mat_solution_b")

    def test_concentration_fact_records_definition_site_binding(self):
        graph, facts = _nife_facts()
        conc_fact = _fact(
            "f_b_conc2", "material_graph[0].material_inputs[1].concentration_value",
            1, DEF_B_SENTENCE, unit="M",
        )
        sink: dict = {}
        issue = self._issue(conc_fact, graph, facts, sink)
        self.assertEqual(issue, "")
        self.assertEqual(
            sink["binding"]["rule_id"], RULE_DEFINITION_SITE_CONCENTRATION,
        )

    def test_ambiguous_state_fact_still_pending(self):
        graph, facts = _nife_facts()
        ambiguous = "The solution was added to the solution slowly."
        state_fact = _fact(
            "f_mix_state", "material_graph[0].material_outputs[0].state",
            "solution", ambiguous,
        )
        sink: dict = {}
        issue = self._issue(state_fact, graph, facts + [state_fact], sink)
        self.assertEqual(issue, "semantic_binding_pending")
        self.assertNotIn("binding", sink)


class ProposalUniqueOwnerIsNotEvidenceTests(unittest.TestCase):
    """Dropping the competing entity from a proposal must not turn a wrong
    or ambiguous state attribution into a pass: bare-label ownership that is
    merely unique inside the proposal's label table is not source evidence."""

    SENTENCE_D = "The solution was cooled overnight before use."
    MIXED = "The suspension in the solution was mixed gently."

    def _proposal(self, reaction_mixture_facts=True):
        graph = _graph(
            _port(0, "inputs", 0, "mat_reaction_mixture", "suspension"),
            _port(0, "inputs", 1, "mat_side", "side product"),
        )
        facts = [
            _fact("f_mix_state", "material_graph[0].material_inputs[0].state",
                  "solution", self.MIXED),
            _fact("f_side_name", "material_graph[0].material_inputs[1].name",
                  "side product", self.MIXED),
        ]
        if reaction_mixture_facts:
            facts.insert(0, _fact(
                "f_mix_name", "material_graph[0].material_inputs[0].name",
                "solution", self.SENTENCE_D,
            ))
        return graph, facts

    def _issue(self, graph, facts):
        source = {
            "paper_id": "paper-A", "experimental_group_id": "Group A",
            "section": "Methods", "locator": "lines:1-1",
            "source_digest": "sha256_" + "a" * 64,
        }
        state_fact = next(f for f in facts
                          if f["field_path"].endswith("inputs[0].state"))
        trial = [dict(fact, source=source) for fact in facts]
        return _fact_issue(
            dict(state_fact, source=source), paper_id="paper-A",
            group_id="Group A", section="Methods",
            document_digest="sha256_" + "a" * 64, graph=graph,
            signature={"route_family": "r", "target_transformation": "t",
                       "endpoint_state": "solution"},
            facts=trial,
        )

    def test_dropping_the_competing_entity_stays_pending(self):
        graph, facts = self._proposal()
        full_issue = self._issue(graph, facts)
        self.assertEqual(full_issue, "semantic_binding_pending")
        # Same source, same field under test: only the competing entity's
        # facts vanish from the proposal.  Ownership becomes unique in the
        # label table, but this passage never anchors the label to the
        # entity, and the legacy matcher must not reopen it.
        reduced = [f for f in facts if not f["fact_id"].startswith("f_mix_name")]
        reduced_graph = _graph(_port(0, "inputs", 0, "mat_reaction_mixture",
                                     "suspension"))
        self.assertEqual(self._issue(reduced_graph, reduced),
                         "semantic_binding_pending")

    def test_same_passage_name_anchor_binds(self):
        # The proposal itself quotes this very passage as the entity's
        # "solution" label: the co-reference is source-located (and review
        # can reject the name fact), so the state claim binds through it.
        graph, facts = self._proposal()
        anchored = [dict(f) for f in facts]
        anchored[0] = _fact(
            "f_mix_name", "material_graph[0].material_inputs[0].name",
            "solution", self.MIXED,
        )
        self.assertEqual(self._issue(graph, anchored), "")

    def test_engaged_failure_does_not_fall_back_to_legacy_matcher(self):
        # The legacy strictly-local matcher would accept this excerpt (one
        # state mention, one name mention, link words only); the association
        # layer engaged (the token is bare-owned and anchored nowhere here)
        # and failed, which must stay pending.
        graph, facts = self._proposal()
        self.assertEqual(self._issue(graph, facts), "semantic_binding_pending")


class SourceContradictsMergeTests(unittest.TestCase):
    """Proposal-internal consistency (same ID, same instance, anchored same
    quote, chained names) must never override an explicit source statement
    that two mentions are not one material."""

    DISTINCT = "The solution and the suspension were two different materials."
    CONVERTED = "The solution was converted to a suspension by aging overnight."

    def _proposal(self, sentence, *, chain=False):
        graph = [{
            "macro_step_id": "m1", "sequence": 2 if chain else 1,
            "operation": "mixed", "sample_id": "s1",
            "material_inputs": [
                {"material_id": "mat_m", "material_instance_id": "inst_m",
                 "name": "solution", "state": "solution"},
                {"material_id": "mat_m", "material_instance_id": "inst_m",
                 "name": "suspension", "state": "suspension"},
            ],
            "material_intermediates": [],
            "material_outputs": [],
        }]
        facts = [
            _fact("f_name_0", "material_graph[0].material_inputs[0].name",
                  "solution", sentence),
            _fact("f_name_1", "material_graph[0].material_inputs[1].name",
                  "suspension", sentence),
            _fact("f_state_0", "material_graph[0].material_inputs[0].state",
                  "solution", sentence),
            _fact("f_state_1", "material_graph[0].material_inputs[1].state",
                  "suspension", sentence),
        ]
        if chain:
            # Extend the model's own merge across neighbouring steps so the
            # graph is internally consistent: same ID, same names, chained.
            for fact in facts:
                fact["field_path"] = fact["field_path"].replace(
                    "material_graph[0]", "material_graph[1]", 1,
                )
            precursor = {
                "macro_step_id": "m0", "sequence": 1, "operation": "prepared",
                "sample_id": "s1", "material_inputs": [],
                "material_intermediates": [],
                "material_outputs": [
                    {"material_id": "mat_m",
                     "material_instance_id": "inst_m_in",
                     "name": "solution", "state": "solution"},
                ],
            }
            consumer = {
                "macro_step_id": "m2", "sequence": 3, "operation": "aged",
                "sample_id": "s1",
                "material_inputs": [
                    {"material_id": "mat_m",
                     "material_instance_id": "inst_m_out",
                     "name": "suspension", "state": "suspension"},
                ],
                "material_intermediates": [],
                "material_outputs": [],
            }
            graph = [precursor, *graph, consumer]
        return graph, facts

    def _issue(self, graph, facts, path):
        source = {
            "paper_id": "paper-A", "experimental_group_id": "Group A",
            "section": "Methods", "locator": "lines:1-1",
            "source_digest": "sha256_" + "a" * 64,
        }
        fact = next(f for f in facts if f["field_path"] == path)
        return _fact_issue(
            dict(fact, source=source), paper_id="paper-A",
            group_id="Group A", section="Methods",
            document_digest="sha256_" + "a" * 64, graph=graph,
            signature={"route_family": "r", "target_transformation": "t",
                       "endpoint_state": "suspension"},
            facts=[dict(f, source=source) for f in facts],
        )

    def test_same_quote_same_id_same_instance_still_rejected(self):
        graph, facts = self._proposal(self.DISTINCT)
        for path in (
            "material_graph[0].material_inputs[0].state",
            "material_graph[0].material_inputs[1].state",
        ):
            self.assertEqual(self._issue(graph, facts, path),
                             "semantic_binding_pending")

    def test_chained_merge_still_rejected(self):
        # Identity-level continuity now holds (earlier output "solution",
        # later input "suspension"); the source contradiction must still
        # block every state fact that quotes it.
        graph, facts = self._proposal(self.DISTINCT, chain=True)
        for path in (
            "material_graph[1].material_inputs[0].state",
            "material_graph[1].material_inputs[1].state",
        ):
            self.assertEqual(self._issue(graph, facts, path),
                             "semantic_binding_pending")

    def test_conversion_statement_remains_attributable(self):
        # The positive control: a source passage describing one material
        # changing state must still attribute; not every bare label pends.
        graph, facts = self._proposal(self.CONVERTED)
        self.assertEqual(
            self._issue(graph, facts, "material_graph[0].material_inputs[0].state"),
            "",
        )
        self.assertEqual(
            self._issue(graph, facts, "material_graph[0].material_inputs[1].state"),
            "",
        )

    def test_explicit_distinctness_detector_bounds(self):
        from chem_agent_contracts.route_source_labels import (
            labels_explicitly_distinct,
        )
        self.assertTrue(labels_explicitly_distinct(
            self.DISTINCT, "solution", "suspension"))
        # Attributive "different" does not merge or split anything.
        self.assertFalse(labels_explicitly_distinct(
            "The solution and the suspension were added to different flasks.",
            "solution", "suspension"))
        self.assertFalse(labels_explicitly_distinct(
            self.CONVERTED, "solution", "suspension"))
        self.assertFalse(labels_explicitly_distinct(
            "The solution and the suspension were combined.",
            "solution", "suspension"))


class LegacyEntryDoesNotEasePassageTests(unittest.TestCase):
    """Removing the name basis of an entity must not make current-format
    input easier to pass: the legacy matcher may answer a fact, but the
    required-field coverage of the strict entry still blocks the group."""

    def _group(self, *, with_name_fact=True):
        source = {
            "paper_id": "paper-A", "experimental_group_id": "Group A",
            "section": "Methods", "locator": "lines:1-1",
            "source_digest": "sha256_" + "a" * 64,
        }
        facts = [
            _fact("f_sig_family", "route_signature.route_family",
                  "precipitation", "The precipitation yields the target solution."),
            _fact("f_sig_transform", "route_signature.target_transformation",
                  "precipitation", "The precipitation yields the target solution."),
            _fact("f_sig_end", "route_signature.endpoint_state",
                  "solution", "The precipitation yields the target solution."),
            _fact("f_state", "material_graph[0].material_inputs[0].state",
                  "solution", "The precursor solution was dissolved."),
            _fact("f_op", "material_graph[0].operation", "dissolved",
                  "The precursor solution was dissolved."),
        ]
        if with_name_fact:
            facts.insert(0, _fact(
                "f_name", "material_graph[0].material_inputs[0].name",
                "precursor solution", "The precursor solution was dissolved.",
            ))
        return {
            "paper_id": "paper-A", "experimental_group_id": "Group A",
            "source": {
                "source_document": "/papers/a.pdf", "section": "Methods",
                "locator": "lines:1-3", "source_digest": "sha256_" + "a" * 64,
            },
            "target": {"material": "product", "desired_state": "solution",
                       "objective": "make"},
            "route_signature": {
                "route_family": "precipitation",
                "target_transformation": "precipitation",
                "endpoint_state": "solution",
            },
            "required_capabilities": ["stir"],
            "material_graph": [{
                "macro_step_id": "S1", "macro_action_id": "A1", "sequence": 1,
                "operation": "dissolved", "sample_id": "arm-1",
                "provenance": {"kind": "paper", "reference": "fact:f_op"},
                "material_inputs": [{
                    "material_id": "mat_p", "material_instance_id": "inst_p",
                    "name": "precursor solution", "state": "solution",
                    "material_origin": "external_inventory",
                    "provenance": {"kind": "paper", "reference": (
                        "fact:f_name" if with_name_fact else "fact:f_state"
                    )},
                }],
                "material_intermediates": [],
                "material_outputs": [],
            }],
            "route_facts": [dict(fact, source=source) for fact in facts],
        }

    def test_state_fact_alone_uses_legacy_matcher(self):
        from chem_agent_contracts.route_source_labels import (
            build_source_label_context, state_attribution_outcome,
        )
        group = self._group(with_name_fact=False)
        graph = group["material_graph"]
        facts = group["route_facts"]
        context = build_source_label_context(graph, facts)
        outcome, binding = state_attribution_outcome(
            "solution", "The precursor solution was dissolved.",
            "material_graph[0].material_inputs[0].state", graph, context,
        )
        self.assertEqual(outcome, "legacy")
        self.assertIsNone(binding)

    def test_missing_name_fact_still_blocked_at_compile(self):
        from reaserch_agent.route_group_compiler import (
            compile_experimental_group_protocols,
        )
        result = compile_experimental_group_protocols([self._group(with_name_fact=False)])
        self.assertEqual(len(result.diagnostics), 1)
        self.assertEqual(
            result.diagnostics[0].reason_code,
            "route_fact_qualitative_graph_claim_missing",
        )


if __name__ == "__main__":
    unittest.main()
