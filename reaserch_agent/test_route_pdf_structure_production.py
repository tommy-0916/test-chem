"""The unsigned PDF producer builds only source-supported split/transfer edges."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from chem_agent_contracts.route_candidate import (
    ExperimentalGroupScopeV1, RouteCandidateV1, RouteGoalV1, RouteTargetV1,
)
from chem_agent_contracts.route_convention_basis import derive_unreviewed_output_state
from chem_agent_contracts.test_route_research_package_draft import (
    _build as build_fixture_package, _reviewed_fixture,
)
from chem_agent_contracts.v2 import (
    EvidenceBundleV2, MacroStepV2, MaterialOperationSegmentV2,
    MaterialRelationV2, ResearchActionPackageV2, canonical_digest,
    route_material_graph_digest_v1,
)
from reaserch_agent.route_discovery import discover_route_candidates
from reaserch_agent.route_group_compiler import compile_experimental_group_protocols
from reaserch_agent.route_group_fact_receipt import produce_pdf_group_fact_receipt
from reaserch_agent.route_pdf_group_extraction import (
    construct_unreviewed_split_transfer_structure,
    propose_pdf_group_unreviewed,
)
from reaserch_agent.route_pdf_local_diagnostics import assess_pdf_group_proposal_fields
from reaserch_agent.route_pdf_groups import (
    PdfExperimentalGroupV1, PdfSourceBlockV1,
    enumerate_pdf_experimental_groups,
)
from reaserch_agent.route_pdf_source import verify_route_pdf_source
from reaserch_agent.route_science import audit_route_candidate_science


class PdfStructureProductionTest(unittest.TestCase):
    @staticmethod
    def _fixture(
        *, phrase: str = "divided into 3 parts", count: int = 3,
        group_id: str = "Portions A", paper_id: str = "paper-division",
        parent_name: str = "suspension", local_material: str = "batch-X",
        local_parent: str = "original-X", child_symbol: str = "portion",
        sentence: str | None = None,
    ) -> tuple[PdfExperimentalGroupV1, dict]:
        quote = sentence or (
            f"In this preparation, the {parent_name} was {phrase} for washing."
        )
        digest = "sha256_" + sha256(
            f"{paper_id}\0{group_id}\0{quote}".encode("utf-8")
        ).hexdigest()
        locator = "pdf:p2:b12-p2:b12"
        group = PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id=paper_id, experimental_group_id=group_id,
                section="Methods", locator=locator, source_digest=digest,
            ),
            source_document="/attested/other-paper.pdf",
            blocks=(PdfSourceBlockV1(locator, quote),),
        )
        outputs = [{
            "material_id": local_material,
            "material_instance_id": f"{child_symbol}-{index}",
            "name": parent_name, "state": "suspension",
            "provenance": {"kind": "paper", "reference": f"fact:out-name-{index}"},
        } for index in range(count)]
        step = {
            "macro_step_id": "S1", "macro_action_id": "A1", "sequence": 1,
            "operation": phrase, "sample_id": "sample-X",
            "provenance": {"kind": "paper", "reference": "fact:operation"},
            "material_inputs": [{
                "material_id": local_material,
                "material_instance_id": local_parent,
                "name": parent_name, "state": "suspension",
                "material_origin": "external_inventory",
                "provenance": {"kind": "paper", "reference": "fact:parent-name"},
            }],
            "material_intermediates": [], "material_outputs": outputs,
            # The model only proposes ports and a source-bound operation.
            # It does not author the segment, edge, lineage, or rule ID.
        }
        facts = []

        def fact(fact_id: str, path: str, value: str) -> None:
            facts.append({
                "fact_id": fact_id, "field_path": path, "value": value,
                "unit": "", "excerpt": quote, "required": True,
            })

        fact("operation", "material_graph[0].operation", phrase)
        fact("parent-name", "material_graph[0].material_inputs[0].name", parent_name)
        fact("parent-state", "material_graph[0].material_inputs[0].state", "suspension")
        for fact_id, path, value in (
            ("signature-family", "route_signature.route_family", parent_name),
            ("signature-change", "route_signature.target_transformation", phrase),
            ("signature-precursor", "route_signature.precursor_roles[0]", parent_name),
            ("signature-operation", "route_signature.operations[0]", phrase),
            ("signature-endpoint", "route_signature.endpoint_state", "suspension"),
        ):
            fact(fact_id, path, value)
        for index in range(count):
            fact(f"out-name-{index}",
                 f"material_graph[0].material_outputs[{index}].name", parent_name)
            fact(f"out-state-{index}",
                 f"material_graph[0].material_outputs[{index}].state", "suspension")
        proposal = {
            "source_group_ref": {
                "paper_id": paper_id, "experimental_group_id": group_id,
                "source_digest": digest,
            },
            "role_hint": "material_processing",
            "target": {
                "material": parent_name, "desired_state": "suspension",
                "objective": f"produce {count} material instance(s)",
            },
            "route_signature": {
                "route_family": parent_name,
                "target_transformation": phrase,
                "precursor_roles": [parent_name], "reagent_roles": [],
                "operations": [phrase], "control_modes": [],
                "phase_transitions": [], "endpoint_state": "suspension",
            },
            "material_graph": [step], "route_facts": facts,
        }
        return group, proposal

    @staticmethod
    def _constructed(group: PdfExperimentalGroupV1, proposal: dict):
        return construct_unreviewed_split_transfer_structure(proposal, group)

    def test_other_source_three_way_split_is_typed_and_source_scoped(self) -> None:
        group, raw = self._fixture()
        untouched = deepcopy(raw)
        prepared, audit, issues = self._constructed(group, raw)
        self.assertEqual(issues, [])
        self.assertEqual(raw, untouched)
        self.assertNotEqual(prepared, raw)
        step = prepared["material_graph"][0]
        relation = MaterialRelationV2.model_validate(
            step["material_relations"][0], strict=True,
        )
        segment = MaterialOperationSegmentV2.model_validate(
            step["operation_segments"][0], strict=True,
        )
        self.assertEqual(relation.event_kind, "split_same_material")
        self.assertEqual(segment.material_effect, "split_material")
        self.assertEqual(relation.quantity_basis, "runtime_measurement_required")
        self.assertEqual(relation.source_operation_ref, segment.segment_id)
        self.assertEqual(step["lineage_relation"]["relation_type"], "split_from_parent")
        parent = step["material_inputs"][0]
        children = step["material_outputs"]
        self.assertEqual(len({port["material_id"] for port in children + [parent]}), 1)
        self.assertEqual(len({port["material_instance_id"] for port in children}), 3)
        self.assertNotIn(parent["material_instance_id"], {
            port["material_instance_id"] for port in children
        })
        self.assertTrue(all("quantity" not in port for port in children))
        self.assertEqual(relation.input_material_instance_ids,
                         [parent["material_instance_id"]])
        self.assertEqual(set(relation.output_material_instance_ids), {
            port["material_instance_id"] for port in children
        })
        self.assertTrue(audit)
        self.assertTrue(any(
            row.get("experimental_group_id") == group.source_scope.experimental_group_id
            and row.get("source_digest") == group.source_scope.source_digest
            for row in audit
        ))
        state_path = "material_graph[0].material_outputs[0].state"
        proof, reason = derive_unreviewed_output_state(
            prepared["material_graph"], prepared["route_facts"], state_path,
            paper_id=group.source_scope.paper_id,
            experimental_group_id=group.source_scope.experimental_group_id,
            source_digest=group.source_scope.source_digest,
        )
        self.assertEqual(reason, "")
        self.assertEqual(proof["rule_id"], "SPLIT_V1")

    def test_transfer_is_one_to_one_and_preserves_material_identity(self) -> None:
        group, raw = self._fixture(
            phrase="transfer", count=1, group_id="Transfer B",
            parent_name="suspension", child_symbol="receiving-vial",
            sentence="In this transfer procedure, we transfer suspension to a vial.",
        )
        prepared, audit, issues = self._constructed(group, raw)
        self.assertEqual(issues, [])
        self.assertTrue(audit)
        step = prepared["material_graph"][0]
        relation = MaterialRelationV2.model_validate(
            step["material_relations"][0], strict=True,
        )
        self.assertEqual(relation.event_kind, "process_same_material")
        self.assertEqual(relation.output_material_instance_ids,
                         [step["material_outputs"][0]["material_instance_id"]])
        self.assertEqual(step["lineage_relation"]["relation_type"], "transfer_of")
        self.assertEqual(step["material_inputs"][0]["material_id"],
                         step["material_outputs"][0]["material_id"])
        self.assertNotEqual(step["material_inputs"][0]["material_instance_id"],
                            step["material_outputs"][0]["material_instance_id"])

    def test_second_paper_and_local_symbols_have_no_fixed_case_ids(self) -> None:
        group, raw = self._fixture(
            phrase="split into 4 parts", count=4,
            group_id="Four portions", paper_id="paper-other-material",
            local_material="local-material-B", local_parent="feed-B",
            child_symbol="fraction-B",
            sentence="The suspension was split into 4 parts for parallel testing.",
        )
        prepared, audit, issues = self._constructed(group, raw)
        self.assertEqual(issues, [])
        self.assertEqual(len(prepared["material_graph"][0]["material_outputs"]), 4)
        self.assertTrue(audit)
        graph = prepared["material_graph"]
        for index in range(4):
            path = f"material_graph[0].material_outputs[{index}].state"
            proof, reason = derive_unreviewed_output_state(
                graph, prepared["route_facts"], path,
                paper_id=group.source_scope.paper_id,
                experimental_group_id=group.source_scope.experimental_group_id,
                source_digest=group.source_scope.source_digest,
            )
            self.assertEqual(reason, "")
            self.assertEqual(proof["rule_id"], "SPLIT_V1")
        assessed = assess_pdf_group_proposal_fields(
            [group], [prepared], check_required_graph_facts=True,
        )
        self.assertFalse([
            issue for issue in assessed.issues
            if issue["reason_code"] in {
                "required_graph_fact_missing", "parent_state_not_child_evidence",
                "semantic_binding_pending", "fact_quantity_role_mismatch",
            }
        ])

    def test_minimal_split_semantics_produce_instances_and_field_facts(self) -> None:
        group, raw = self._fixture(
            phrase="split into 3 parts", count=3,
            group_id="Only parent described", paper_id="independent-paper",
            local_parent="feed", local_material="material-X",
            sentence="The suspension was split into 3 parts for parallel washing.",
        )
        raw["material_graph"][0]["material_outputs"] = []
        raw["material_graph"][0]["material_inputs"][0].pop("material_origin", None)
        raw["route_facts"] = [
            fact for fact in raw["route_facts"]
            if not fact["field_path"].startswith("material_graph[0].material_outputs[")
        ]
        original = deepcopy(raw)
        prepared, audit, issues = self._constructed(group, raw)
        self.assertEqual(issues, [])
        self.assertEqual(raw, original)
        step = prepared["material_graph"][0]
        parent = step["material_inputs"][0]
        self.assertNotIn("material_origin", parent)
        children = step["material_outputs"]
        self.assertEqual(len(children), 3)
        self.assertEqual(len({child["material_instance_id"] for child in children}), 3)
        self.assertTrue(all(
            child["material_instance_id"] != parent["material_instance_id"]
            and child["material_id"] == parent["material_id"]
            and child["name"] == parent["name"]
            and child["state"] == parent["state"]
            and "quantity" not in child
            for child in children
        ))
        facts_by_path = {fact["field_path"]: fact
                         for fact in prepared["route_facts"]}
        for index in range(3):
            name_path = f"material_graph[0].material_outputs[{index}].name"
            state_path = f"material_graph[0].material_outputs[{index}].state"
            self.assertEqual(facts_by_path[name_path]["value"], parent["name"])
            self.assertEqual(facts_by_path[state_path]["excerpt"],
                             facts_by_path["material_graph[0].operation"]["excerpt"])
            proof, reason = derive_unreviewed_output_state(
                prepared["material_graph"], prepared["route_facts"], state_path,
                paper_id=group.source_scope.paper_id,
                experimental_group_id=group.source_scope.experimental_group_id,
                source_digest=group.source_scope.source_digest,
            )
            self.assertEqual(reason, "")
            self.assertEqual(proof["rule_id"], "SPLIT_V1")
        self.assertTrue(audit)
        self.assertTrue(any(
            str(change.get("field_path", "")).endswith(".material_instance_id")
            for row in audit for change in row.get("field_changes", [])
        ))
        assessed = assess_pdf_group_proposal_fields(
            [group], [prepared], check_required_graph_facts=True,
        )
        self.assertFalse([
            issue for issue in assessed.issues
            if issue["reason_code"] in {
                "required_graph_fact_missing", "parent_state_not_child_evidence",
                "semantic_binding_pending", "fact_quantity_role_mismatch",
            }
        ])

    def test_missing_parent_duplicate_children_and_count_mismatch_abstain(self) -> None:
        for mutation in ("missing_parent", "duplicate_children", "count_mismatch"):
            group, raw = self._fixture()
            if mutation == "missing_parent":
                raw["material_graph"][0]["material_inputs"] = []
            elif mutation == "duplicate_children":
                outputs = raw["material_graph"][0]["material_outputs"]
                outputs[1]["material_instance_id"] = outputs[0]["material_instance_id"]
            else:
                raw["material_graph"][0]["material_outputs"].pop()
            original = deepcopy(raw)
            prepared, audit, issues = self._constructed(group, raw)
            with self.subTest(mutation=mutation):
                self.assertEqual(raw, original)
                self.assertTrue(issues)
                self.assertFalse(prepared["material_graph"][0].get("material_relations"))

    def test_explicit_conflicting_relation_is_not_silently_replaced(self) -> None:
        group, raw = self._fixture()
        raw["material_graph"][0]["material_relations"] = [{
            "relation_id": "claimed-edge", "event_kind": "split_same_material",
            "input_material_instance_ids": ["different-parent"],
            "output_material_instance_ids": ["portion-0", "portion-1", "portion-2"],
            "quantity_basis": "runtime_measurement_required",
            "source_operation_ref": "claimed-segment",
            "provenance": {"kind": "paper", "reference": "fact:operation"},
        }]
        original = deepcopy(raw)
        prepared, _audit, issues = self._constructed(group, raw)
        self.assertEqual(raw, original)
        self.assertTrue(issues)
        self.assertEqual(
            prepared["material_graph"][0]["material_relations"],
            original["material_graph"][0]["material_relations"],
        )

    def test_prior_instance_from_other_sample_cannot_be_new_inventory(self) -> None:
        group, raw = self._fixture()
        later = raw["material_graph"][0]
        earlier_port = deepcopy(later["material_inputs"][0])
        earlier_port.pop("material_origin", None)
        earlier = {
            "macro_step_id": "S0", "macro_action_id": "A0", "sequence": 0,
            "operation": "preparation", "sample_id": "other-sample-arm",
            "material_inputs": [], "material_intermediates": [],
            "material_outputs": [earlier_port],
            "provenance": {"kind": "paper", "reference": "fact:earlier"},
        }
        raw["material_graph"].insert(0, earlier)
        for fact in raw["route_facts"]:
            fact["field_path"] = fact["field_path"].replace(
                "material_graph[0]", "material_graph[1]"
            )
        original = deepcopy(raw)
        prepared, _audit, issues = self._constructed(group, raw)
        self.assertEqual(raw, original)
        self.assertIn("structure_parent_upstream_scope_ambiguous", {
            issue["reason_code"] for issue in issues
        })
        self.assertFalse(prepared["material_graph"][1].get("material_relations"))

    def test_existing_child_fact_with_foreign_excerpt_is_not_rewritten(self) -> None:
        for broken_fact in ("child_name", "child_state"):
            group, raw = self._fixture()
            state_path = "material_graph[0].material_outputs[0].state"
            name_path = "material_graph[0].material_outputs[0].name"
            if broken_fact == "child_name":
                raw["material_graph"][0]["material_outputs"][0]["name"] = "parts"
                target_path = name_path
                next(fact for fact in raw["route_facts"]
                     if fact["field_path"] == target_path)["value"] = "parts"
            else:
                target_path = state_path
            next(fact for fact in raw["route_facts"]
                 if fact["field_path"] == target_path)["excerpt"] = (
                "The parts in another paper had this description."
            )
            original = deepcopy(raw)
            prepared, _audit, issues = self._constructed(group, raw)
            with self.subTest(broken_fact=broken_fact):
                self.assertEqual(raw, original)
                self.assertTrue(issues)
                self.assertFalse(prepared["material_graph"][0].get("material_relations"))
                self.assertEqual(
                    next(fact for fact in prepared["route_facts"]
                         if fact["field_path"] == target_path)["excerpt"],
                    "The parts in another paper had this description.",
                )

    def test_two_distinct_split_events_in_same_group_cannot_be_conflated(self) -> None:
        first = "The suspension was divided into 3 parts for washing."
        second = "Later, the suspension was divided into 3 parts for analysis."
        original_group, raw = self._fixture(sentence=first)
        group = PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id=original_group.source_scope.paper_id,
                experimental_group_id=original_group.source_scope.experimental_group_id,
                section="Methods", locator="pdf:p2:b12-p2:b13",
                source_digest=original_group.source_scope.source_digest,
            ),
            source_document=original_group.source_document,
            blocks=(
                PdfSourceBlockV1("pdf:p2:b12-p2:b12", first),
                PdfSourceBlockV1("pdf:p2:b13-p2:b13", second),
            ),
        )
        for fact in raw["route_facts"]:
            if fact["field_path"].startswith("material_graph[0].material_outputs["):
                fact["excerpt"] = second
        snapshot = deepcopy(raw)
        prepared, _audit, issues = self._constructed(group, raw)
        self.assertEqual(raw, snapshot)
        self.assertTrue(issues)
        self.assertFalse(prepared["material_graph"][0].get("material_relations"))
        self.assertTrue(all(
            fact["excerpt"] == second
            for fact in prepared["route_facts"]
            if fact["field_path"].startswith("material_graph[0].material_outputs[")
        ))

    def test_negated_operation_and_other_group_quote_abstain(self) -> None:
        cases = (
            ("We did not split suspension into 3 parts.", "split"),
            ("The suspension was not divided into 3 parts.", "divided into 3 parts"),
        )
        for sentence, operation in cases:
            group, raw = self._fixture(phrase=operation, sentence=sentence)
            _prepared, _audit, issues = self._constructed(group, raw)
            with self.subTest(sentence=sentence):
                self.assertTrue(issues)
        group, raw = self._fixture()
        other = PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id=group.source_scope.paper_id,
                experimental_group_id="Other Group",
                section=group.source_scope.section,
                locator="pdf:p3:b5-p3:b5",
                source_digest=group.source_scope.source_digest,
            ),
            source_document=group.source_document,
            blocks=(PdfSourceBlockV1(
                "pdf:p3:b5-p3:b5",
                "The unrelated emulsion was divided into 3 parts.",
            ),),
        )
        _prepared, _audit, issues = self._constructed(other, raw)
        self.assertTrue(issues)

    def test_plurally_referred_parents_and_wrong_operation_subject_abstain(self) -> None:
        # The cited ER group says "samples were transferred" across multiple
        # preparations; the text alone does not select one parent instance.
        group, raw = self._fixture(
            phrase="transferred", count=1,
            sentence=("The suspension samples were transferred to a "
                      "Teflon-lined stainless-steel autoclave."),
        )
        step = raw["material_graph"][0]
        second_parent = deepcopy(step["material_inputs"][0])
        second_parent["material_instance_id"] = "other-arm"
        step["material_inputs"].append(second_parent)
        _prepared, _audit, issues = self._constructed(group, raw)
        self.assertTrue(issues)

        group, raw = self._fixture(
            phrase="transferred", count=1,
            sentence=("The nickel foam was transferred to an oven; "
                      "the suspension remained in a vial."),
        )
        _prepared, _audit, issues = self._constructed(group, raw)
        self.assertTrue(issues)

    def test_count_and_concentration_cannot_become_child_amounts(self) -> None:
        for value, unit in ((3, "parts"), (1, "M")):
            group, raw = self._fixture()
            output = raw["material_graph"][0]["material_outputs"][0]
            output["quantity"] = {"mode": "exact", "value": value, "unit": unit}
            raw["route_facts"].append({
                "fact_id": "wrong-amount",
                "field_path": "material_graph[0].material_outputs[0].quantity.value",
                "value": value, "unit": unit, "excerpt": group.blocks[0].text,
                "required": True,
            })
            _prepared, _audit, issues = self._constructed(group, raw)
            with self.subTest(unit=unit):
                self.assertTrue(issues)

    def test_count_of_water_parts_cannot_supply_missing_split_cardinality(self) -> None:
        sentence = (
            "The suspension was divided into portions and washed with "
            "3 parts water."
        )
        group, raw = self._fixture(
            phrase="divided into portions", count=3, sentence=sentence,
        )
        raw["material_graph"][0]["material_outputs"] = []
        raw["route_facts"] = [
            fact for fact in raw["route_facts"]
            if "material_outputs" not in fact["field_path"]
        ]
        original = deepcopy(raw)
        prepared, audit, issues = self._constructed(group, raw)
        self.assertEqual(raw, original)
        self.assertEqual(audit, [])
        self.assertIn("structure_split_count_or_children_unresolved",
                      {issue["reason_code"] for issue in issues})
        self.assertEqual(prepared["material_graph"][0]["material_outputs"], [])

    def test_source_bound_count_becomes_child_cardinality_with_audit(self) -> None:
        group, raw = self._fixture()
        raw["material_graph"][0]["material_outputs"] = []
        raw["route_facts"] = [
            fact for fact in raw["route_facts"]
            if "material_outputs" not in fact["field_path"]
        ]
        raw["material_graph"][0]["count"] = 3
        count_fact = {
            "fact_id": "asserted-part-count",
            "field_path": "material_graph[0].count", "value": 3,
            "unit": "", "excerpt": group.blocks[0].text, "required": True,
        }
        raw["route_facts"].append(count_fact)
        original = deepcopy(raw)
        prepared, audit, issues = self._constructed(group, raw)
        self.assertEqual(issues, [])
        self.assertEqual(raw, original)
        step = prepared["material_graph"][0]
        self.assertNotIn("count", step)
        self.assertEqual(len(step["material_outputs"]), 3)
        self.assertFalse(any(
            fact["field_path"] == "material_graph[0].count"
            for fact in prepared["route_facts"]
        ))
        count_change = next(
            change for change in audit[0]["field_changes"]
            if change["field_path"] == "material_graph[0].count"
        )
        self.assertEqual(count_change["from"]["value"], 3)
        self.assertEqual(count_change["from"]["fact"], count_fact)
        self.assertEqual(count_change["to"]["child_instance_count"], 3)
        self.assertFalse(any("quantity" in output for output in step["material_outputs"]))

    def test_bad_count_fact_or_transfer_count_cannot_be_reinterpreted(self) -> None:
        for asserted, value, unit, excerpt in (
            (4, 4, "", None),
            (3, 4, "", None),
            (3, 3, "parts", None),
            (3, 3, "", "Unrelated parts were collected elsewhere."),
        ):
            with self.subTest(asserted=asserted, value=value, unit=unit,
                              excerpt=excerpt):
                group, raw = self._fixture()
                raw["material_graph"][0]["material_outputs"] = []
                raw["route_facts"] = [
                    fact for fact in raw["route_facts"]
                    if "material_outputs" not in fact["field_path"]
                ]
                raw["material_graph"][0]["count"] = asserted
                raw["route_facts"].append({
                    "fact_id": "asserted-part-count",
                    "field_path": "material_graph[0].count", "value": value,
                    "unit": unit, "excerpt": excerpt or group.blocks[0].text,
                    "required": True,
                })
                original = deepcopy(raw)
                prepared, audit, issues = self._constructed(group, raw)
                self.assertEqual(raw, original)
                self.assertEqual(audit, [])
                self.assertIn("structure_split_count_fact_invalid",
                              {issue["reason_code"] for issue in issues})
                self.assertEqual(prepared["material_graph"][0]["count"], asserted)

        group, transfer = self._fixture(
            phrase="transferred", count=1,
            sentence="The suspension was transferred to a clean vial.",
        )
        transfer["material_graph"][0]["count"] = 1
        _prepared, audit, issues = self._constructed(group, transfer)
        self.assertEqual(audit, [])
        self.assertIn("structure_transfer_count_unsupported",
                      {issue["reason_code"] for issue in issues})

    @unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF unavailable")
    def test_minimal_model_semantics_reach_source_science_and_v2_rebuild(self) -> None:
        """A generated field chain is checked; this is no human review or publish."""
        import fitz

        sentence = "The suspension was split into 3 parts for parallel washing."
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "independent-source.pdf"
            document = fitz.open()
            page = document.new_page()
            for index, (body, size, font) in enumerate((
                ("Methods", 16, "hebo"),
                ("Group A", 14, "hebo"),
                (sentence, 10, "helv"),
            )):
                page.insert_text((72, 70 + index * 48), body,
                                 fontsize=size, fontname=font)
            document.save(str(pdf))
            document.close()
            indexed = enumerate_pdf_experimental_groups(
                {"paper-independent": pdf}, source_root=root,
            )
            self.assertEqual(indexed.diagnostics, [])
            self.assertEqual(len(indexed.groups), 1)
            group = indexed.groups[0]
            _mock_group, raw = self._fixture(
                phrase="split into 3 parts", count=3,
                paper_id="paper-independent", group_id="Group A",
                sentence=sentence,
            )
            raw["source_group_ref"] = {
                "paper_id": group.source_scope.paper_id,
                "experimental_group_id": group.source_scope.experimental_group_id,
                "source_digest": group.source_scope.source_digest,
            }
            raw["material_graph"][0]["material_outputs"] = []
            raw["material_graph"][0]["material_inputs"][0].pop(
                "material_origin", None
            )
            raw["route_facts"] = [
                fact for fact in raw["route_facts"]
                if not fact["field_path"].startswith("material_graph[0].material_outputs[")
            ]
            raw_snapshot = deepcopy(raw)
            produced = propose_pdf_group_unreviewed(
                [group], lambda _prompt: {"proposals": [raw]},
                max_repair_groups=0, check_required_graph_facts=True,
            )
            self.assertEqual(raw, raw_snapshot)
            self.assertEqual(produced.diagnostics, [])
            self.assertTrue(produced.locator_production["material_structure"])
            self.assertEqual(len(produced.protocols), 1)
            unreviewed = produced.protocols[0]
            self.assertEqual(unreviewed["group_role"], "unclassified")
            self.assertEqual(len(unreviewed["material_graph"][0]["material_outputs"]), 3)
            self.assertIsNone(unreviewed["material_graph"][0]["material_inputs"][0]
                              .get("material_origin"))
            receipt = produce_pdf_group_fact_receipt(
                [group], [unreviewed], signed_inventory_verified=True,
            )
            self.assertEqual(receipt.status,
                             "literal_facts_verified_pending_review")
            state_path = "material_graph[0].material_outputs[0].state"
            self.assertIn(state_path,
                          receipt.group_results[0].derived_state_field_paths)
            # A synthetic independent role/capability mapping exercises later
            # type boundaries. It is not a chemical review receipt.
            reviewed_for_test = deepcopy(unreviewed)
            reviewed_for_test["group_role"] = "synthesis"
            reviewed_for_test["required_capabilities"] = ["split"]
            compiled = compile_experimental_group_protocols([reviewed_for_test])
            self.assertEqual(compiled.diagnostics, [])
            bound = compiled.protocols[0]
            child_field = next(item for item in bound["evidence_matrix"]
                               if item["field_path"] == state_path)
            self.assertEqual(child_field["provenance"]["evidence_class"],
                             "chemistry_convention")
            proof = json.loads(child_field["provenance"]["derivation"])
            goal = RouteGoalV1(
                goal_id="goal", target=RouteTargetV1.model_validate(bound["target"]),
                constraint="open", required_fields=[state_path],
            )
            discovered = discover_route_candidates(
                goal, compiled.protocols,
                trusted_source_paths={"paper-independent": [pdf]},
            )
            self.assertEqual(discovered.diagnostics, [])
            self.assertEqual(len(discovered.candidates), 1)
            candidate = discovered.candidates[0]
            verified = verify_route_pdf_source(
                candidate, source_paths={"paper-independent": pdf},
                source_root=root,
            )
            self.assertTrue(verified.source_scope_verified, verified.reasons)
            self.assertNotIn(state_path, verified.verified_field_paths)
            self.assertIn(proof["parent_state_path"], verified.verified_field_paths)
            self.assertIn(proof["operation_path"], verified.verified_field_paths)
            science_data = candidate.model_dump(mode="json")
            for item in science_data["evidence_bundle"]:
                if item["evidence_id"] in verified.verified_evidence_ids:
                    item["verification_status"] = "local_file"
                    item["full_text_status"] = "local_parsed"
            science = audit_route_candidate_science(
                RouteCandidateV1.model_validate(science_data)
            )
            self.assertIn(state_path, science["audited_field_paths"])
            self.assertIn(state_path, science["verified_convention_field_paths"])

            # The Research package still uses a synthetic reviewed fixture.
            # Only the generated graph and its exact evidence/proof cross V2.
            draft, decision, bundle = _reviewed_fixture()
            baseline = build_fixture_package(draft, decision, bundle)
            payload = baseline.model_dump(mode="json", exclude_none=True)
            payload.pop("research_contract_hash")
            step = deepcopy(bound["material_graph"][0])
            step["macro_action_id"] = baseline.macro_action.macro_action_id
            step["sample_id"] = baseline.macro_action.experiment_group.sample_id
            payload["macro_steps"] = [step]
            payload["evidence_bundle"]["items"] = [{
                **item, "verification_status": "verified_doi",
                "full_text_status": "parsed",
            } for item in bound["evidence_bundle"]]
            binding = payload["route_binding"]
            binding["source_paper_id"] = group.source_scope.paper_id
            binding["experimental_group_id"] = group.source_scope.experimental_group_id
            binding["source_digest"] = group.source_scope.source_digest
            binding["convention_state_proofs"] = [
                json.loads(item["provenance"]["derivation"])
                for item in bound["evidence_matrix"]
                if item["field_path"].startswith("material_graph[0].material_outputs[")
                and item["field_path"].endswith(".state")
            ]
            binding["material_graph_digest"] = route_material_graph_digest_v1([
                MacroStepV2.model_validate(step, strict=True),
            ])
            binding["evidence_bundle_digest"] = canonical_digest(
                EvidenceBundleV2.model_validate(payload["evidence_bundle"],
                                                strict=True)
            )
            binding["intent_digest"] = canonical_digest({
                "schema_version": "route-action-intent/v1",
                "route_id": binding["route_id"],
                "candidate_digest": binding["candidate_digest"],
                "decision_id": binding["decision_id"],
                "evidence_bundle_id": payload["evidence_bundle"]["bundle_id"],
                "evidence_bundle_digest": binding["evidence_bundle_digest"],
                "stage": payload["stage"], "macro_action": payload["macro_action"],
            })
            built = ResearchActionPackageV2.model_validate(payload, strict=True)
            wire = built.model_dump(mode="json", exclude_none=True)
            wire.pop("research_contract_hash")
            rebuilt = ResearchActionPackageV2.model_validate(
                json.loads(json.dumps(wire)), strict=True,
            )
            self.assertEqual(
                rebuilt.route_binding.convention_state_proofs[0].model_dump(
                    mode="json"
                ), proof,
            )


if __name__ == "__main__":
    unittest.main()
