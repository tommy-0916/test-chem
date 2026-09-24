"""A paper's recipe may support arithmetic without stating the result verbatim."""

from __future__ import annotations

import copy
import unittest

from chem_agent_contracts.v2 import (
    MacroStepV2,
    ResearchActionPackageV2,
    canonical_digest,
    evidence_contains_exact_quantity,
    validate_literature_calculation,
)
from chem_agent_contracts.test_v2 import research_fixture


EXCERPT = (
    "Ni(NO3)2∙6H2O, Fe(NO3)3∙9H2O and urea were dissolved in 80 ml of "
    "water to a final concentration of 7.5, 2.5 and 17.5 mM, respectively."
)
ORDERED = ["Ni(NO3)2∙6H2O", "Fe(NO3)3∙9H2O", "urea"]


def paper_provenance() -> dict:
    return {
        "kind": "paper",
        "reference": "current_evidence_1",
        "source_path": "evidence_bundle.items[0].excerpt",
        "excerpt": EXCERPT,
        "source_digest": canonical_digest(EXCERPT),
    }


def calculated_requirement(
    *, subject: str = ORDERED[0], material_id: str = "NI",
    material: str = "Ni(NO3)2·6H2O reagent", concentration: float = 7.5,
    amount: float = 0.6,
) -> dict:
    return {
        "kind": "scientific_input_setpoint",
        "material_id": material_id,
        "material": material,
        "value": amount,
        "unit": "mmol",
        "source": "literature_calculation",
        "provenance": paper_provenance(),
        "derivation": {
            "rule": "mM_times_mL_to_mmol_v1",
            "ordered_materials": list(ORDERED),
            "material_evidence_name": subject,
            "concentration_value": concentration,
            "concentration_unit": "mM",
            "volume_value": 80,
            "volume_unit": "mL",
        },
    }


def macro_step(requirement: dict, *, port_value: float) -> dict:
    return {
        "macro_step_id": "MS_1",
        "macro_action_id": "MA_1",
        "sequence": 1,
        "operation": "dissolve precursors",
        "sample_id": "S_1",
        "material_contract_status": {
            "material_inputs": "unresolved",
            "material_intermediates": "unresolved",
            "material_outputs": "unresolved",
            "logical_containers": "unresolved",
            "material_relations": "unresolved",
        },
        "material_inputs": [{
            "material_id": requirement["material_id"],
            "material_instance_id": "input_1",
            "name": requirement["material"],
            "state": "solid",
            "quantity": {
                "mode": "exact", "semantic": "planned_target",
                "value": port_value, "unit": "mmol",
            },
            "material_origin": "external_inventory",
            "provenance": paper_provenance(),
        }],
        "quantity_requirements": [requirement],
        "provenance": paper_provenance(),
    }


class LiteratureCalculationTest(unittest.TestCase):
    @staticmethod
    def package_payload(requirement: dict) -> dict:
        payload = research_fixture().model_dump(mode="json")
        payload["research_contract_hash"] = ""
        step = macro_step(requirement, port_value=requirement["value"])
        step["macro_action_id"] = payload["macro_action"]["macro_action_id"]
        step["sample_id"] = payload["macro_action"]["experiment_group"]["sample_id"]
        payload["macro_steps"] = [step]
        payload["evidence_bundle"]["items"] = [{
            "evidence_id": "current_evidence_1",
            "verification_status": "local_file",
            "full_text_status": "local_parsed",
            "excerpt": EXCERPT,
        }]
        return payload

    def test_ordered_shared_unit_list_supports_all_three_amounts(self):
        for subject, material_id, material, concentration, amount in (
            (ORDERED[0], "NI", "Ni(NO3)2·6H2O reagent", 7.5, 0.6),
            (ORDERED[1], "FE", "Fe(NO3)3·9H2O reagent", 2.5, 0.2),
            (ORDERED[2], "UREA", "urea", 17.5, 1.4),
        ):
            with self.subTest(subject=subject):
                requirement = calculated_requirement(
                    subject=subject, material_id=material_id,
                    material=material, concentration=concentration, amount=amount,
                )
                validate_literature_calculation(
                    requirement, requirement["provenance"]
                )
                MacroStepV2.model_validate(
                    macro_step(requirement, port_value=amount)
                )

    def test_calculated_amount_is_not_a_directly_quoted_paper_quantity(self):
        self.assertFalse(evidence_contains_exact_quantity(EXCERPT, 0.6, "mmol"))
        requirement = calculated_requirement()
        requirement["source"] = "literature"
        with self.assertRaisesRegex(ValueError, "distinct source label"):
            validate_literature_calculation(requirement, requirement["provenance"])

    def test_alternative_mix_wording_is_not_tied_to_one_paper(self):
        requirement = calculated_requirement()
        excerpt = (
            "Ni(NO3)2∙6H2O, Fe(NO3)3∙9H2O and urea were mixed with 80 mL "
            "water at final concentrations of 7.5, 2.5, and 17.5 mM, respectively."
        )
        requirement["provenance"]["excerpt"] = excerpt
        requirement["provenance"]["source_digest"] = canonical_digest(excerpt)
        validate_literature_calculation(requirement, requirement["provenance"])

    def test_rejects_swapped_material_concentration_pair(self):
        requirement = calculated_requirement(
            subject=ORDERED[1], material_id="NI", material="Ni(NO3)2·6H2O reagent",
            concentration=2.5, amount=0.2,
        )
        with self.assertRaisesRegex(ValueError, "subject does not match"):
            validate_literature_calculation(requirement, requirement["provenance"])

        requirement = calculated_requirement(concentration=2.5, amount=0.2)
        with self.assertRaisesRegex(ValueError, "respectively material binding"):
            validate_literature_calculation(requirement, requirement["provenance"])

    def test_rejects_arithmetic_or_source_tampering(self):
        valid = calculated_requirement()
        for mutation in (
            lambda item: item.update(value=0.7),
            lambda item: item["derivation"].update(volume_value=70),
            lambda item: item["provenance"].update(
                excerpt=EXCERPT.replace("respectively", "in any order")
            ),
            lambda item: item["provenance"].update(source_digest=""),
            lambda item: item["derivation"].update(concentration_unit="mol/L"),
        ):
            item = copy.deepcopy(valid)
            mutation(item)
            with self.subTest(item=item), self.assertRaises(ValueError):
                validate_literature_calculation(item, item["provenance"])

    def test_unique_external_port_must_match_derived_requirement(self):
        requirement = calculated_requirement()
        with self.assertRaisesRegex(ValueError, "exact quantity conflicts"):
            MacroStepV2.model_validate(macro_step(requirement, port_value=0.7))

    def test_frozen_package_accepts_calculation_but_not_false_paper_quote(self):
        payload = self.package_payload(calculated_requirement())
        package = ResearchActionPackageV2.model_validate(payload)
        self.assertEqual(
            package.macro_steps[0].quantity_requirements[0]["value"], 0.6
        )

        direct_claim = self.package_payload(calculated_requirement())
        direct_claim["macro_steps"][0]["quantity_requirements"][0][
            "source"
        ] = "literature"
        direct_claim["macro_steps"][0]["quantity_requirements"][0][
            "material"
        ] = ORDERED[0]
        direct_claim["macro_steps"][0]["material_inputs"][0]["name"] = ORDERED[0]
        with self.assertRaisesRegex(ValueError, "exact value/unit pair"):
            ResearchActionPackageV2.model_validate(direct_claim)

        stale_source = self.package_payload(calculated_requirement())
        stale_source["macro_steps"][0]["quantity_requirements"][0][
            "provenance"
        ]["source_digest"] = "sha256_" + "0" * 64
        with self.assertRaisesRegex(ValueError, "source_digest does not match"):
            ResearchActionPackageV2.model_validate(stale_source)


if __name__ == "__main__":
    unittest.main()
