from __future__ import annotations

import unittest

from chem_agent_contracts.v2 import canonical_digest
from reaserch_agent.workflow import ResearchAgent


EXCERPT = (
    "compound A, compound B and urea were dissolved in 80 ml of water to a final "
    "concentration of 7.5, 2.5 and 17.5 mM, respectively."
)


def requirement(*, concentration: float = 7.5, value: float = 0.6) -> dict:
    return {
        "kind": "scientific_input_setpoint",
        "material_id": "compound_a",
        "material": "compound A",
        "value": value,
        "unit": "mmol",
        "source": "literature_calculation",
        "derivation": {
            "rule": "mM_times_mL_to_mmol_v1",
            "ordered_materials": ["compound A", "compound B", "urea"],
            "material_evidence_name": "compound A",
            "concentration_value": concentration,
            "concentration_unit": "mM",
            "volume_value": 80,
            "volume_unit": "mL",
        },
        "provenance": {
            "kind": "paper",
            "reference": "source-1",
            "source_path": "evidence_bundle.items[0].excerpt",
            "excerpt": EXCERPT,
            "source_digest": canonical_digest(EXCERPT),
        },
    }


class ResearchLiteratureCalculationTest(unittest.TestCase):
    def _issues(self, claim: dict) -> list[str]:
        return ResearchAgent._v2_quantity_requirement_issues(
            1,
            {
                "material_inputs": [{
                    "material_id": "compound_a",
                    "name": "compound A",
                    "quantity": {
                        "mode": "exact", "semantic": "planned_target",
                        "value": claim["value"], "unit": claim["unit"],
                    },
                }],
                "material_intermediates": [],
                "material_outputs": [],
                "quantity_requirements": [claim],
            },
        )

    def test_checked_derivation_is_not_misrepresented_as_quoted_dose(self):
        self.assertEqual(self._issues(requirement()), [])

        direct_paper_claim = requirement()
        direct_paper_claim["source"] = "literature"
        self.assertTrue(
            any("未包含完全匹配" in issue for issue in self._issues(direct_paper_claim))
        )

    def test_swapped_reagent_concentration_and_bad_arithmetic_are_rejected(self):
        swapped = requirement(concentration=2.5, value=0.2)
        self.assertTrue(any("文献计算验证失败" in issue for issue in self._issues(swapped)))

        bad_result = requirement(value=0.61)
        self.assertTrue(any("文献计算验证失败" in issue for issue in self._issues(bad_result)))

    def test_calculated_dose_must_match_input_port_quantity(self):
        claim = requirement()
        step = {
            "material_inputs": [{
                "material_id": "compound_a", "name": "compound A",
                "quantity": {
                    "mode": "exact", "semantic": "planned_target",
                    "value": 0.2, "unit": "mmol",
                },
            }],
            "material_intermediates": [],
            "material_outputs": [],
            "quantity_requirements": [claim],
        }
        issues = ResearchAgent._v2_quantity_requirement_issues(1, step)
        self.assertTrue(any("与唯一主动输入端口" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
