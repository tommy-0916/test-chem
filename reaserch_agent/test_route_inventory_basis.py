"""External-input state source resolution: four acceptance cases.

The resolver binds an external input's state only to an approved
inventory / material-spec record that uniquely matches the concrete
material with a compatible spec.  It never guesses, never upgrades an
inventory record into a paper fact, and refuses ports whose upstream
lineage contradicts an ``external_inventory`` label.
"""

from __future__ import annotations

import json
import unittest

from chem_agent_contracts.route_inventory_basis import (
    AMBIGUOUS,
    LINEAGE_BLOCKED,
    RESOLVED,
    SOURCE_MISSING,
    SPEC_MISMATCH,
    apply_inventory_resolutions,
    load_inventory_resource,
    resolve_external_input_states,
    verified_resolutions,
)
from chem_agent_contracts.route_candidate import (
    RouteCandidateV1, RouteFieldEvidenceV1, RouteSignatureV1, RouteTargetV1,
)
from chem_agent_contracts.route_decision import _field_issue as decision_field_issue
from reaserch_agent.route_group_compiler import (
    compile_experimental_group_protocols,
)

DIGEST = "sha256_" + "a" * 64

ITEMS = [
    {
        "item_id": "inv_ni_nitrate",
        "material_name": "Ni(NO3)2·6H2O",
        "supply_form": "powder",
        "record": "vendor catalog: crystalline hydrate, ACS reagent grade",
    },
    {
        "item_id": "inv_naoh_solid",
        "material_name": "NaOH",
        "supply_form": "powder",
        "record": "stock room: solid pellets, anhydrous",
    },
    {
        "item_id": "inv_naoh_solution",
        "material_name": "NaOH",
        "supply_form": "solution",
        "concentration_value": 1.0,
        "concentration_unit": "M",
        "record": "prep log: 1 M NaOH solution",
    },
    {
        "item_id": "inv_water",
        "material_name": "water",
        "supply_form": "solution",
        "record": "Milli-Q system, 15 MΩ/cm",
    },
]


def _graph(origin="external_inventory", *, name="Ni(NO3)2·6H2O",
           quantity_unit="mmol", upstream_output=False,
           operation="dissolved"):
    port = {
        "material_id": "mat_m", "material_instance_id": "inst_m",
        "name": name, "state": None,
        "material_origin": origin,
        "quantity": {"value": 37.5, "unit": quantity_unit},
        "provenance": {"kind": "paper", "reference": "fact:f_name"},
    }
    if upstream_output:
        port["parent_output_refs"] = [{"macro_step_id": "m0", "material_instance_id": "i0"}]
    return [{
        "macro_step_id": "m1", "macro_action_id": "a1", "sequence": 1,
        "operation": operation,
        "sample_id": "arm-1",
        "provenance": {"kind": "paper", "reference": "fact:f_op"},
        "material_inputs": [port],
        "material_intermediates": [],
        "material_outputs": [],
    }]


class ResolverClassificationTests(unittest.TestCase):
    def test_unique_inventory_match_resolves_with_real_source(self):
        records = resolve_external_input_states(_graph(), ITEMS)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["status"], RESOLVED)
        self.assertEqual(record["item_id"], "inv_ni_nitrate")
        self.assertEqual(record["state"], "powder")
        self.assertIn("vendor catalog", record["record"])

    def test_missing_inventory_is_an_explicit_dependency(self):
        records = resolve_external_input_states(_graph(), [])
        self.assertEqual([r["status"] for r in records], [SOURCE_MISSING])

    def test_solution_item_cannot_satisfy_dissolving_operation(self):
        # Only a 1 M NaOH solution is stocked, and the source operation
        # explicitly dissolves the NaOH: a bare mmol amount could still be
        # dosed from the solution, but the operation constraint requires a
        # non-solution supply form, so no record may be picked.
        solution_only = [item for item in ITEMS
                         if item["item_id"] == "inv_naoh_solution"]
        graph = _graph(name="NaOH", operation="prepared by dissolving NaOH")
        records = resolve_external_input_states(graph, solution_only)
        self.assertEqual([r["status"] for r in records], [SPEC_MISMATCH])
        resolved = apply_inventory_resolutions(_graph(name="NaOH"), records)
        self.assertIsNone(resolved[0]["material_inputs"][0]["state"])

    def test_solution_item_resolves_without_operation_constraint(self):
        # With no dissolving statement, the same input may bind the
        # solution record: a bare mmol amount never proves a form by itself.
        solution_only = [item for item in ITEMS
                         if item["item_id"] == "inv_naoh_solution"]
        records = resolve_external_input_states(_graph(name="NaOH"), solution_only)
        self.assertEqual(
            [(r["status"], r.get("state")) for r in records],
            [(RESOLVED, "solution")],
        )

    def test_two_stocked_forms_disambiguate_by_operation_constraint(self):
        # Stocked both as pellets and as 1 M solution, with an explicit
        # dissolving operation: the solid record is the compatible one.
        graph = _graph(name="NaOH", operation="prepared by dissolving NaOH")
        records = resolve_external_input_states(graph, ITEMS)
        self.assertEqual(
            [(r["status"], r.get("item_id")) for r in records],
            [(RESOLVED, "inv_naoh_solid")],
        )

    def test_two_stocked_forms_without_constraint_are_ambiguous(self):
        graph = _graph(name="NaOH")
        records = resolve_external_input_states(graph, ITEMS)
        self.assertEqual([r["status"] for r in records], [AMBIGUOUS])
        self.assertEqual(len(records[0]["candidates"]), 2)

    def test_unique_solution_item_with_matching_amount_unit_resolves(self):
        # Water has a unique record; a mL dosing amount is compatible.
        records = resolve_external_input_states(
            _graph(name="water", quantity_unit="mL"), ITEMS)
        self.assertEqual(
            [(r["status"], r.get("state")) for r in records],
            [(RESOLVED, "solution")],
        )

    def test_upstream_evidence_blocks_inventory_shortcut(self):
        graph = _graph(upstream_output=True)
        records = resolve_external_input_states(graph, ITEMS)
        self.assertEqual([r["status"] for r in records], [LINEAGE_BLOCKED])

    def test_produced_output_upstream_blocks_inventory_shortcut(self):
        graph = _graph(origin="external_inventory")
        graph.insert(0, {
            "macro_step_id": "m0", "sequence": 0, "operation": "made",
            "sample_id": "arm-1", "material_inputs": [],
            "material_intermediates": [],
            "material_outputs": [{
                "material_id": "mat_m", "material_instance_id": "i0",
                "name": "Ni(NO3)2·6H2O", "state": "solution",
            }],
        })
        records = resolve_external_input_states(graph, ITEMS)
        self.assertEqual(
            [r["status"] for r in records
             if r["field_path"].startswith("material_graph[1]")],
            [LINEAGE_BLOCKED],
        )


class VerifiedResolutionsTests(unittest.TestCase):
    def test_verified_requires_live_item_and_matching_state(self):
        records = resolve_external_input_states(_graph(), ITEMS)
        digest = "sha256_" + "b" * 64
        for record in records:
            record["register"] = "material-inventory/v1"
            record["register_digest"] = digest
        verified = verified_resolutions(records, ITEMS, digest)
        self.assertEqual(
            list(verified),
            ["material_graph[0].material_inputs[0].state"],
        )
        stale = verified_resolutions(records, [], digest)
        self.assertEqual(stale, {})
        mutated = [dict(item, supply_form="solution") for item in ITEMS]
        self.assertEqual(verified_resolutions(records, mutated, digest), {})
        wrong_digest = [dict(record, register_digest="sha256_" + "d" * 64)
                        for record in records]
        self.assertEqual(verified_resolutions(wrong_digest, ITEMS, digest), {})

    def test_loader_rejects_wrong_schema(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "inv.json"
            bad.write_text(json.dumps({"schema": "other/v1", "items": []}),
                           encoding="utf-8")
            self.assertEqual(load_inventory_resource(bad), ([], ""))
            good = Path(tmp) / "good.json"
            good.write_text(json.dumps({
                "schema": "material-inventory/v1", "items": ITEMS[:1],
            }), encoding="utf-8")
            items, digest = load_inventory_resource(good)
            self.assertEqual(len(items), 1)
            self.assertTrue(digest.startswith("sha256_"))


def _resolved_group():
    source = {
        "paper_id": "paper-A", "experimental_group_id": "Group A",
        "section": "Methods", "locator": "lines:1-1",
        "source_digest": DIGEST,
    }
    graph = _graph()
    records = resolve_external_input_states(graph, ITEMS)
    graph = apply_inventory_resolutions(graph, records)
    digest = "sha256_" + "b" * 64
    for record in records:
        record["register"] = "material-inventory/v1"
        record["register_digest"] = digest
    verified = verified_resolutions(records, ITEMS, digest)
    resolutions = []
    for record in records:
        if record["status"] != RESOLVED:
            continue
        info = verified[record["field_path"]]
        resolutions.append({
            "field_path": record["field_path"],
            "status": "resolved",
            "item_id": record["item_id"],
            "supply_form": record["supply_form"],
            "state": record["state"],
            "record": record["record"],
            "register": "material-inventory/v1",
            "register_digest": digest,
        })
    quote = ("37.5 mmol Ni(NO3)2·6H2O was dissolved in water to give a solution.")
    facts = [
        {"fact_id": "f_name", "field_path": "material_graph[0].material_inputs[0].name",
         "value": "Ni(NO3)2·6H2O", "unit": "", "excerpt": quote,
         "required": True, "source": source},
        {"fact_id": "f_op", "field_path": "material_graph[0].operation",
         "value": "dissolved", "unit": "", "excerpt": quote,
         "required": True, "source": source},
        {"fact_id": "f_qty", "field_path": "material_graph[0].material_inputs[0].quantity.value",
         "value": 37.5, "unit": "mmol", "excerpt": quote,
         "required": True, "source": source},
        {"fact_id": "f_sig", "field_path": "route_signature.route_family",
         "value": "dissolved", "unit": "", "excerpt": quote,
         "required": True, "source": source},
        {"fact_id": "f_sig2", "field_path": "route_signature.target_transformation",
         "value": "dissolved", "unit": "", "excerpt": quote,
         "required": True, "source": source},
        {"fact_id": "f_sig3", "field_path": "route_signature.endpoint_state",
         "value": "solution", "unit": "", "excerpt": quote,
         "required": True, "source": source},
    ]
    return {
        "paper_id": "paper-A", "experimental_group_id": "Group A",
        "source": {
            "source_document": "/papers/a.pdf", "section": "Methods",
            "locator": "lines:1-3", "source_digest": DIGEST,
        },
        "target": {"material": "product", "desired_state": "solution",
                   "objective": "make"},
        "route_signature": {
            "route_family": "dissolved", "target_transformation": "dissolved",
            "endpoint_state": "solution",
        },
        "required_capabilities": ["stir"],
        "material_graph": graph,
        "inventory_resolutions": resolutions,
        "route_facts": facts,
    }


class CompileAndDecisionGateTests(unittest.TestCase):
    def test_compiled_inventory_state_is_not_a_paper_fact(self):
        result = compile_experimental_group_protocols([_resolved_group()])
        self.assertEqual(result.diagnostics, [])
        group = result.protocols[0]
        row = next(
            item for item in group["evidence_matrix"]
            if item["field_path"] == "material_graph[0].material_inputs[0].state"
        )
        provenance = row["provenance"]
        self.assertEqual(provenance["kind"], "inventory")
        self.assertEqual(provenance["evidence_class"], "inventory_record")
        self.assertEqual(provenance["reference"], "inv_ni_nitrate")
        self.assertNotEqual(provenance["evidence_class"], "paper_explicit")
        self.assertEqual(row["evidence_id"], "")

    def test_decision_gate_accepts_verified_inventory_state(self):
        result = compile_experimental_group_protocols([_resolved_group()])
        group = result.protocols[0]
        candidate = RouteCandidateV1.model_validate({
            "route_id": "r1",
            "target": {"material": "product", "desired_state": "solution",
                       "objective": "make"},
            "route_signature": group["route_signature"],
            "evidence_bundle": group["evidence_bundle"],
            "evidence_matrix": group["evidence_matrix"],
            "material_graph": group["material_graph"],
            "required_capabilities": ["stir"],
            "origin": "paper_experimental_group",
            "source_scope": {
                "paper_id": "paper-A", "experimental_group_id": "Group A",
                "section": "Methods", "locator": "lines:1-3",
                "source_digest": DIGEST,
            },
        })
        row = next(
            item for item in candidate.evidence_matrix
            if item.field_path == "material_graph[0].material_inputs[0].state"
        )
        issue = decision_field_issue(row, candidate, receipt=None)
        self.assertIsNone(issue)

    def test_decision_gate_rejects_inventory_provenance_without_record(self):
        result = compile_experimental_group_protocols([_resolved_group()])
        group = result.protocols[0]
        matrix = json.loads(json.dumps(group["evidence_matrix"]))
        for row in matrix:
            if row["field_path"].endswith(".state"):
                row["provenance"] = {
                    "kind": "inventory", "reference": "inv_ni_nitrate",
                    "evidence_class": "inventory_record", "excerpt": "x",
                    "source_digest": "not-a-digest",
                }
        candidate = RouteCandidateV1.model_validate({
            "route_id": "r1",
            "target": {"material": "product", "desired_state": "solution",
                       "objective": "make"},
            "route_signature": group["route_signature"],
            "evidence_bundle": group["evidence_bundle"],
            "evidence_matrix": matrix,
            "material_graph": group["material_graph"],
            "required_capabilities": ["stir"],
            "origin": "paper_experimental_group",
            "source_scope": {
                "paper_id": "paper-A", "experimental_group_id": "Group A",
                "section": "Methods", "locator": "lines:1-3",
                "source_digest": DIGEST,
            },
        })
        row = next(
            item for item in candidate.evidence_matrix
            if item.field_path.endswith(".state")
        )
        self.assertEqual(
            decision_field_issue(row, candidate, receipt=None),
            "inventory_digest_missing",
        )

    def test_g1_accepts_verified_register_resolution(self):
        from reaserch_agent.route_pdf_local_diagnostics import (
            assess_pdf_group_proposal_fields,
        )
        group = _resolved_group()
        group.pop("inventory_resolutions")
        graph = group["material_graph"]
        facts = group["route_facts"]
        records = resolve_external_input_states(graph, ITEMS, facts)
        digest = "sha256_" + "b" * 64
        resolutions = [{
            "field_path": record["field_path"],
            "status": "resolved",
            "item_id": record["item_id"],
            "supply_form": record["supply_form"],
            "state": record["state"],
            "record": record["record"],
            "register": "material-inventory/v1",
            "register_digest": digest,
        } for record in records if record["status"] == RESOLVED]
        assessment = assess_pdf_group_proposal_fields(
            [], [group], check_required_graph_facts=True,
            inventory_registers={
                "material-inventory/v1": (ITEMS, digest, tuple(resolutions)),
            },
        )
        self.assertEqual(
            [i for i in assessment.issues
             if i["reason_code"] == "required_graph_fact_missing"],
            [],
        )
        # A resolution that does not re-verify against the bundled bytes
        # discharges nothing.
        bad = assess_pdf_group_proposal_fields(
            [], [group], check_required_graph_facts=True,
            inventory_registers={
                "material-inventory/v1": (ITEMS, "sha256_" + "c" * 64,
                                          tuple(resolutions)),
            },
        )
        self.assertTrue(any(
            i["reason_code"] == "required_graph_fact_missing"
            for i in bad.issues
        ))

    def test_save_reload_preserves_inventory_source_binding(self):
        import json as jsonlib
        result = compile_experimental_group_protocols([_resolved_group()])
        group = result.protocols[0]
        candidate = RouteCandidateV1.model_validate({
            "route_id": "r1",
            "target": {"material": "product", "desired_state": "solution",
                       "objective": "make"},
            "route_signature": group["route_signature"],
            "evidence_bundle": group["evidence_bundle"],
            "evidence_matrix": group["evidence_matrix"],
            "material_graph": group["material_graph"],
            "required_capabilities": ["stir"],
            "origin": "paper_experimental_group",
            "source_scope": {
                "paper_id": "paper-A", "experimental_group_id": "Group A",
                "section": "Methods", "locator": "lines:1-3",
                "source_digest": DIGEST,
            },
        })
        saved = jsonlib.loads(jsonlib.dumps(candidate.model_dump(mode="json")))
        reloaded = RouteCandidateV1.model_validate(saved)
        row = next(
            item for item in reloaded.evidence_matrix
            if item.field_path == "material_graph[0].material_inputs[0].state"
        )
        self.assertEqual(row.provenance.kind, "inventory")
        self.assertEqual(row.provenance.evidence_class, "inventory_record")
        self.assertEqual(decision_field_issue(row, reloaded, receipt=None), None)

    def test_unresolved_state_still_blocks_compile(self):
        group = _resolved_group()
        group["inventory_resolutions"] = []
        group["material_graph"][0]["material_inputs"][0]["state"] = None
        result = compile_experimental_group_protocols([group])
        self.assertEqual(
            [d.reason_code for d in result.diagnostics],
            ["route_group_material_state_missing"],
        )


if __name__ == "__main__":
    unittest.main()
