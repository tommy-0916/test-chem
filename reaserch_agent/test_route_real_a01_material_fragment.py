"""A frozen real A01 precursor field chain, without route publication.

The two PDF blocks and raw proposal fields are copied from the A01 local
replay. Their local full-source files are checked when available, while the
committed fragment runs in a fresh clone. The fragment supplies neither a
complete experimental group nor an independent chemistry review.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import unittest

from chem_agent_contracts.route_candidate import (
    ExperimentalGroupScopeV1, RouteFieldEvidenceV1,
)
from chem_agent_contracts.v2 import (
    EvidenceItemV2, MaterialPortV2, ProvenanceV2, QuantityV2,
    canonical_digest,
)
from reaserch_agent.route_group_compiler import (
    canonicalize_proposal_material_ids,
    material_id_graph_issue,
)
from reaserch_agent.route_pdf_group_extraction import propose_pdf_group_unreviewed
from reaserch_agent.route_pdf_groups import (
    PdfExperimentalGroupV1,
    PdfSourceBlockV1,
    enumerate_pdf_experimental_groups,
)
from reaserch_agent.route_pdf_local_diagnostics import (
    assess_pdf_group_proposal_fields,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "reaserch_agent" / "fixtures" / "a01_nife_precursor_fragment.json"
REPLAY = ROOT / "result" / "a01-v5-real-input-20260927"
PDF_DIGEST = (
    "sha256_2e9435bccb2e2e281edc0fcbd0ff27ffe48b8c0babcc5e346fbdc0d679df12c8"
)
PDF = REPLAY / "kb" / "_pdf_sources" / "a01_v5" / f"{PDF_DIGEST[7:]}.pdf"
STATE = (
    REPLAY / "campaigns" / "A01-v5-quote-context-20260927"
    / "iteration_00" / "research_state.json"
)
GROUP_ID = "Synthesis of the Pristine Ni3Fe LDHs (NiFe Control)."
NAME_PATH = "material_graph[0].material_inputs[0].name"
QUANTITY_PATH = "material_graph[0].material_inputs[0].quantity.value"
ID_PATH = "material_graph[0].material_inputs[0].material_id"
STATE_PATH = "material_graph[0].material_inputs[0].state"


def _open_state_dependencies(diagnostics) -> list:
    """required_graph_fact_missing rows for the fragment's unresolved state.

    G1 requirements no longer depend on the model having filled a non-empty
    value, so this fragment's missing port state surfaces as an explicit
    dependency instead of vanishing from the coverage check.
    """
    return [
        diagnostic for diagnostic in diagnostics
        if diagnostic.reason_code == "required_graph_fact_missing"
    ]


class RealA01PrecursorFragmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.frozen = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.original = cls.frozen["proposal"]
        scope = ExperimentalGroupScopeV1.model_validate(
            cls.frozen["source_scope"], strict=True,
        )
        cls.group = PdfExperimentalGroupV1(
            source_scope=scope,
            source_document=str(FIXTURE),
            blocks=tuple(PdfSourceBlockV1(**block) for block in cls.frozen["blocks"]),
        )

    def test_frozen_fragment_matches_local_sources_when_available(self) -> None:
        self.assertEqual(self.frozen["schema_version"], "a01_nife_precursor_fragment_v1")
        self.assertEqual(self.group.source_scope.source_digest, PDF_DIGEST)
        self.assertEqual(self.group.source_scope.experimental_group_id, GROUP_ID)
        if PDF.is_file() and importlib.util.find_spec("fitz"):
            self.assertEqual("sha256_" + sha256(PDF.read_bytes()).hexdigest(), PDF_DIGEST)
            enumerated = enumerate_pdf_experimental_groups(
                {"doi_10_1021_acsami_3c11651": PDF}, source_root=PDF.parent,
            )
            self.assertEqual(enumerated.diagnostics, [])
            full_group = next(
                group for group in enumerated.groups
                if group.source_scope.experimental_group_id == GROUP_ID
            )
            self.assertEqual(full_group.source_scope, self.group.source_scope)
            live_blocks = {block.locator: block for block in full_group.blocks}
            for frozen_block in self.group.blocks:
                self.assertEqual(live_blocks[frozen_block.locator], frozen_block)
        if STATE.is_file():
            saved = json.loads(STATE.read_text(encoding="utf-8"))
            full_proposal = next(
                proposal for proposal in saved["raw_llm_outputs"][
                    "route_pdf_group_propose"
                ]["proposals"]
                if proposal["source_group_ref"]["experimental_group_id"] == GROUP_ID
            )
            self.assertEqual(full_proposal["source_group_ref"],
                             self.original["source_group_ref"])
            live_facts = {fact["fact_id"]: fact for fact in full_proposal["route_facts"]}
            for frozen_fact in self.original["route_facts"]:
                self.assertEqual(live_facts[frozen_fact["fact_id"]], frozen_fact)
            for frozen_step, live_step in zip(
                self.original["material_graph"],
                (full_proposal["material_graph"][0], full_proposal["material_graph"][3]),
            ):
                self.assertEqual(frozen_step["macro_step_id"], live_step["macro_step_id"])
                for kind in ("material_inputs", "material_outputs"):
                    for frozen_port, live_port in zip(
                        frozen_step.get(kind, []), live_step.get(kind, []),
                    ):
                        for key, value in frozen_port.items():
                            self.assertEqual(live_port[key], value)

    def _fragment(self, *, quantity: float = 37.5,
                  quantity_excerpt: str | None = None) -> dict:
        source_port = self.original["material_graph"][0]["material_inputs"][0]
        self.assertEqual(source_port["name"], "Ni(NO3)2·6H2O")
        self.assertEqual(source_port["quantity"], {"value": 37.5, "unit": "mmol"})
        source_facts = {item["field_path"]: item for item in self.original["route_facts"]}
        facts = [deepcopy(source_facts[path]) for path in (NAME_PATH, QUANTITY_PATH)]
        self.assertIsNone(facts[0]["unit"], "preserve the saved model's raw unit")
        facts[1]["value"] = quantity
        if quantity_excerpt is not None:
            facts[1]["excerpt"] = quantity_excerpt
        port = {
            "material_id": source_port["material_id"],
            "name": source_port["name"],
            "quantity": {"value": quantity, "unit": source_port["quantity"]["unit"]},
        }
        return {
            "source_group_ref": deepcopy(self.original["source_group_ref"]),
            "role_hint": self.original["role_hint"],
            "material_graph": [{"material_inputs": [port]}],
            "route_facts": facts,
        }

    def _strict_entry(self, fragment: dict):
        envelope = {"proposals": [fragment]}
        original = deepcopy(envelope)
        result = propose_pdf_group_unreviewed(
            [self.group], lambda _prompt: envelope,
            check_required_graph_facts=True,
        )
        self.assertEqual(envelope, original, "strict entry must preserve the raw proposal")
        return result

    def test_strict_field_check_accepts_generated_id_and_literal_ni_amount(self) -> None:
        fragment = self._fragment()
        self.assertFalse(any(
            fact["field_path"] == ID_PATH for fact in fragment["route_facts"]
        ))
        assessment = assess_pdf_group_proposal_fields(
            [self.group], [fragment], check_required_graph_facts=True,
        )
        self.assertEqual(assessment.located.diagnostics, [])
        self.assertIn("fact_unit_invalid", {
            issue["reason_code"] for issue in assessment.issues
        })
        produced = self._strict_entry(fragment)
        # The fragment deliberately carries no state fact; G1 now lists
        # the port state as an explicit required dependency.
        self.assertEqual(
            [item.reason_code for item in produced.diagnostics],
            ["required_graph_fact_missing"],
        )
        self.assertEqual(produced.protocols, [])
        normalized = produced.locator_production["qualitative_unit_normalizations"]
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["field_path"], NAME_PATH)
        self.assertEqual(normalized[0]["from"], "null")
        self.assertEqual(fragment["route_facts"][0]["unit"], None)
        generated_rows = produced.locator_production["generated_material_ids"]
        self.assertEqual(len(generated_rows), 1)
        generated_id = generated_rows[0]["generated_id"]
        self.assertTrue(generated_id)
        self.assertNotEqual(generated_id, fragment["material_graph"][0][
            "material_inputs"
        ][0]["material_id"])
        repeated = self._strict_entry(fragment)
        self.assertEqual(
            repeated.locator_production["generated_material_ids"][0][
                "generated_id"
            ],
            generated_id,
        )

    def test_unit_normalization_does_not_invent_physical_units(self) -> None:
        for bad_unit in (None, {"name": "mmol"}, ["mmol"]):
            with self.subTest(bad_unit=bad_unit):
                fragment = self._fragment()
                fragment["route_facts"][1]["unit"] = bad_unit
                produced = propose_pdf_group_unreviewed(
                    [self.group], lambda _prompt: {"proposals": [fragment]},
                    max_repair_groups=0, check_required_graph_facts=True,
                )
                self.assertEqual(produced.protocols, [])
                self.assertIn("fact_unit_invalid", {
                    diagnostic.reason_code for diagnostic in produced.diagnostics
                })
                self.assertFalse(any(
                    row["field_path"] == QUANTITY_PATH
                    for row in produced.locator_production[
                        "qualitative_unit_normalizations"
                    ]
                ))

    def test_text_unit_object_and_numeric_string_are_not_normalized(self) -> None:
        for unit in ({"unit": ""}, [""]):
            with self.subTest(unit=unit):
                fragment = self._fragment()
                fragment["route_facts"][0]["unit"] = unit
                produced = propose_pdf_group_unreviewed(
                    [self.group], lambda _prompt: {"proposals": [fragment]},
                    max_repair_groups=0, check_required_graph_facts=True,
                )
                self.assertEqual(produced.protocols, [])
                self.assertIn("fact_unit_invalid", {
                    diagnostic.reason_code for diagnostic in produced.diagnostics
                })
        fragment = self._fragment()
        fragment["route_facts"][1]["value"] = "37.5"
        fragment["route_facts"][1]["unit"] = None
        produced = propose_pdf_group_unreviewed(
            [self.group], lambda _prompt: {"proposals": [fragment]},
            max_repair_groups=0, check_required_graph_facts=True,
        )
        self.assertEqual(produced.protocols, [])
        self.assertFalse(any(
            row["field_path"] == QUANTITY_PATH
            for row in produced.locator_production["qualitative_unit_normalizations"]
        ))

    def test_generated_ids_close_real_solution_a_reference(self) -> None:
        original = deepcopy(self.original)
        canonical, audit = canonicalize_proposal_material_ids(original)
        self.assertEqual(self.original, original)
        graph = canonical["material_graph"]
        ni_id = graph[0]["material_inputs"][0]["material_id"]
        solution_a_id = graph[0]["material_outputs"][0]["material_id"]
        reused_solution_a_id = graph[1]["material_inputs"][0]["material_id"]
        self.assertTrue(ni_id)
        self.assertNotEqual(ni_id, solution_a_id)
        self.assertEqual(solution_a_id, reused_solution_a_id)
        self.assertEqual(graph[0]["material_outputs"][0]["name"], "solution A")
        self.assertEqual(graph[1]["material_inputs"][0]["name"], "solution A")
        self.assertIn(
            {
                "field_path": ID_PATH,
                "id_kind": "material_id",
                "model_symbol": "ni_nitrate",
                "generated_id": ni_id,
            },
            audit,
        )

        # Reusing the Ni identity for a distinct Fe precursor must not become
        # valid merely because both strings receive program-generated IDs.
        malformed = deepcopy(self.original)
        malformed["material_graph"][0]["material_inputs"][1]["material_id"] = (
            malformed["material_graph"][0]["material_inputs"][0]["material_id"]
        )
        bad_graph = canonicalize_proposal_material_ids(malformed)[0]["material_graph"]
        self.assertEqual(
            material_id_graph_issue(bad_graph), "route_group_material_id_identity_conflict",
        )

    def test_generated_instance_reference_overlay_stays_closed(self) -> None:
        # The saved unreviewed proposal predates instance IDs and material
        # relations. Add only structural symbols to its real Solution A graph
        # to exercise canonical references; this is not chemistry approval.
        proposal = deepcopy(self.original)
        prepare = proposal["material_graph"][0]
        transfer = proposal["material_graph"][1]
        input_symbols = ["ni_batch", "fe_batch", "water_a_batch"]
        for port, symbol in zip(prepare["material_inputs"], input_symbols):
            port["material_instance_id"] = symbol
        prepare["material_outputs"][0]["material_instance_id"] = "solution_a_batch"
        prepare["material_relations"] = [{
            "input_material_instance_ids": input_symbols,
            "output_material_instance_ids": ["solution_a_batch"],
        }]
        transfer["material_inputs"][0]["material_instance_id"] = "solution_a_transfer"
        transfer["material_inputs"][0]["parent_output_refs"] = [{
            "macro_step_id": prepare["macro_step_id"],
            "material_instance_id": "solution_a_batch",
        }]

        canonical, audit = canonicalize_proposal_material_ids(proposal)
        prepared = canonical["material_graph"][0]
        transferred = canonical["material_graph"][1]
        input_ids = [port["material_instance_id"] for port in prepared["material_inputs"]]
        output_id = prepared["material_outputs"][0]["material_instance_id"]
        relation = prepared["material_relations"][0]
        self.assertEqual(relation["input_material_instance_ids"], input_ids)
        self.assertEqual(relation["output_material_instance_ids"], [output_id])
        self.assertEqual(
            transferred["material_inputs"][0]["parent_output_refs"][0][
                "material_instance_id"
            ], output_id,
        )
        self.assertNotEqual(
            transferred["material_inputs"][0]["material_instance_id"], output_id,
        )
        self.assertIn({
            "field_path": "material_graph[0].material_inputs[0].material_instance_id",
            "id_kind": "material_instance_id",
            "model_symbol": "ni_batch",
            "generated_id": input_ids[0],
        }, audit)

    def test_other_precursor_amount_in_same_sentence_remains_blocked(self) -> None:
        whole_sentence = next(
            fact["excerpt"] for fact in self.original["route_facts"]
            if fact["fact_id"] == "pc_sig1"
        )
        fragment = self._fragment(quantity=12.5, quantity_excerpt=whole_sentence)
        assessment = assess_pdf_group_proposal_fields(
            [self.group], [fragment], check_required_graph_facts=True,
        )
        self.assertIn(
            (QUANTITY_PATH, "fact_quantity_attribution_unresolved"),
            {(issue["field_path"], issue["reason_code"]) for issue in assessment.issues},
        )
        self.assertNotIn((0, 1), assessment.passing_fact_slots)
        produced = self._strict_entry(fragment)
        self.assertEqual(produced.protocols, [])
        self.assertIn(
            "fact_quantity_attribution_unresolved",
            [item.reason_code for item in produced.diagnostics],
        )

    def test_local_revision_normalizes_qualitative_unit_without_changing_raw(self) -> None:
        whole_sentence = next(
            fact["excerpt"] for fact in self.original["route_facts"]
            if fact["fact_id"] == "pc_sig1"
        )
        initial = self._fragment(quantity=12.5, quantity_excerpt=whole_sentence)
        revised = self._fragment()
        calls = []

        def invoke(_prompt: str) -> dict:
            calls.append(_prompt)
            return {"proposals": [deepcopy(initial if len(calls) == 1 else revised)]}

        produced = propose_pdf_group_unreviewed(
            [self.group], invoke, max_repair_groups=1,
            check_required_graph_facts=True,
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(initial["route_facts"][0]["unit"], None)
        self.assertEqual(revised["route_facts"][0]["unit"], None)
        # The missing required state claim cannot be honestly
        # repaired by rewording; the revision is refused, not merged.
        # The fragment deliberately carries no state fact; G1 now lists
        # the port state as an explicit required dependency.
        self.assertEqual(
            [item.reason_code for item in produced.diagnostics],
            ["required_graph_fact_missing",
             "fact_quantity_attribution_unresolved"],
        )
        self.assertEqual(produced.protocols, [])
        revision = produced.locator_production["local_revision"]["revisions"][0]
        self.assertEqual(
            revision["reason_code"],
            "local_revision_required_graph_fact_missing",
        )
        self.assertEqual(
            revision["producer_normalizations"]["qualitative_unit_normalizations"]
            [0]["field_path"], NAME_PATH,
        )

    def test_ni_material_port_v2_json_round_trip_preserves_binding(self) -> None:
        fragment = self._fragment()
        produced = self._strict_entry(fragment)
        # The fragment deliberately carries no state fact; G1 now lists
        # the port state as an explicit required dependency.
        self.assertEqual(
            [item.reason_code for item in produced.diagnostics],
            ["required_graph_fact_missing"],
        )
        source_port = dict(fragment["material_graph"][0]["material_inputs"][0])
        source_port["material_id"] = produced.locator_production[
            "generated_material_ids"
        ][0]["generated_id"]
        compiled_facts = {
            fact["field_path"]: fact for fact in fragment["route_facts"]
        }
        self.assertEqual(source_port["name"], compiled_facts[NAME_PATH]["value"])
        self.assertEqual(source_port["quantity"]["value"], compiled_facts[QUANTITY_PATH]["value"])
        scope_data = self.group.source_scope.model_dump(mode="json")
        self.assertEqual(scope_data["source_digest"], PDF_DIGEST)
        self.assertEqual(
            compiled_facts[QUANTITY_PATH]["block_locator"], "pdf:p2:b58-p2:b58",
        )
        name_fact, quantity_fact = fragment["route_facts"]
        scope_data = self.group.source_scope.model_dump(mode="json")
        source_objects = []
        for index, fact in enumerate((name_fact, quantity_fact)):
            evidence_id = "route_fact_" + sha256(
                (scope_data["paper_id"] + "\0"
                 + scope_data["experimental_group_id"] + "\0"
                 + fact["fact_id"]).encode("utf-8")
            ).hexdigest()[:24]
            evidence = EvidenceItemV2(
                evidence_id=evidence_id, excerpt=fact["excerpt"],
            )
            provenance = ProvenanceV2(
                kind="paper", reference=evidence_id,
                evidence_class="paper_explicit",
                source_path=f"evidence_bundle.items[{index}].excerpt",
                excerpt=fact["excerpt"],
                source_digest=canonical_digest(fact["excerpt"]),
            )
            field = RouteFieldEvidenceV1(
                field_path=fact["field_path"], value=fact["value"],
                unit=fact["unit"] or "", status="supported", required=True,
                evidence_id=evidence_id, provenance=provenance,
                source_scope=ExperimentalGroupScopeV1(
                    **{**scope_data, "locator": fact["block_locator"]}
                ),
            )
            source_objects.append((evidence, field))
        (name_evidence, name_field), (quantity_evidence, quantity_field) = source_objects
        port = MaterialPortV2(
            material_id=source_port["material_id"],
            material_instance_id=f"{source_port['material_id']}-batch-1",
            name=source_port["name"], state="unknown",
            material_origin="external_inventory",
            quantity=QuantityV2(
                mode="exact", semantic="planned_target",
                value=source_port["quantity"]["value"],
                unit=source_port["quantity"]["unit"],
            ),
            provenance=name_field.provenance,
        )
        saved = json.loads(json.dumps({
            "port": port.model_dump(mode="json"),
            "evidence_bundle": [
                name_evidence.model_dump(mode="json"),
                quantity_evidence.model_dump(mode="json"),
            ],
            "fields": [
                name_field.model_dump(mode="json"),
                quantity_field.model_dump(mode="json"),
            ],
        }, ensure_ascii=False))
        rebuilt = MaterialPortV2.model_validate(saved["port"], strict=True)
        reloaded_evidence = [
            EvidenceItemV2.model_validate(item, strict=True)
            for item in saved["evidence_bundle"]
        ]
        reloaded_fields = [
            RouteFieldEvidenceV1.model_validate(item, strict=True)
            for item in saved["fields"]
        ]
        self.assertEqual(rebuilt, port)
        self.assertEqual(reloaded_fields, [name_field, quantity_field])
        self.assertEqual(rebuilt.name, "Ni(NO3)2·6H2O")
        self.assertEqual((rebuilt.quantity.value, rebuilt.quantity.unit), (37.5, "mmol"))
        self.assertEqual(rebuilt.provenance, reloaded_fields[0].provenance)
        self.assertNotEqual(reloaded_fields[0].provenance,
                            reloaded_fields[1].provenance)
        self.assertEqual(
            reloaded_fields[1].provenance.reference,
            reloaded_evidence[1].evidence_id,
        )
        self.assertEqual(
            reloaded_fields[1].provenance.source_digest,
            canonical_digest(quantity_fact["excerpt"]),
        )
        self.assertEqual(reloaded_fields[1].source_scope.source_digest, PDF_DIGEST)
        self.assertNotIn(rebuilt.material_id, rebuilt.provenance.excerpt)


if __name__ == "__main__":
    unittest.main()
