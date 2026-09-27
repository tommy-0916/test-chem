from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from reaserch_agent.observation_protocol import resolve_observation_protocol


class ObservationProtocolTests(unittest.TestCase):
    def test_xrd_uses_bound_skill_and_checks_static_inputs(self) -> None:
        result = resolve_observation_protocol(
            "XRD 物相观察", sample_state="suspension",
            container_type="进样瓶", liquid_volume_ml=4.0,
        )
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.station_code, "XRD_V1")
        self.assertEqual(result.capability_id, "xrd")
        self.assertEqual(result.minimum_liquid_volume_ml, 4.0)
        self.assertFalse(result.minimum_volume_strict)
        self.assertEqual(result.required_capabilities, ("xrd",))
        self.assertTrue(result.protocol_id.startswith("observation-protocol/v1/sha256_"))
        self.assertEqual(len(result.source_skill_sha256), 64)
        self.assertIn(
            "Spectroscopy_Container_Transfer_Station_V1",
            result.required_predecessor_station_codes,
        )

    def test_unknown_xrd_inputs_wait_for_existing_device_state_path(self) -> None:
        result = resolve_observation_protocol("XRD")
        self.assertEqual(result.status, "runtime_pending")
        self.assertTrue(result.protocol_id)
        self.assertEqual(
            result.reason_codes,
            (
                "observation_sample_state_pending",
                "observation_container_pending",
                "observation_volume_pending",
            ),
        )
        self.assertTrue(any("initial_state.containers[*].sample_state" in path for path in result.resolver_path))
        self.assertTrue(any("initial_state.containers[*].volume_ml" in path for path in result.resolver_path))

    def test_xrd_rejects_powder_or_too_little_liquid(self) -> None:
        powder = resolve_observation_protocol(
            "XRD", sample_state="dry_solid",
            container_type="进样瓶", liquid_volume_ml=4.0,
        )
        self.assertEqual(powder.status, "unsupported")
        self.assertIn("observation_sample_state_conflict", powder.reason_codes)
        low_volume = resolve_observation_protocol(
            "XRD", sample_state="suspension",
            container_type="进样瓶", liquid_volume_ml=3.9,
        )
        self.assertEqual(low_volume.status, "unsupported")
        self.assertIn("observation_volume_below_minimum", low_volume.reason_codes)

    def test_other_measurement_protocol_uses_same_path_without_paper_result(self) -> None:
        result = resolve_observation_protocol(
            "UV-Vis absorption", sample_state="solution",
            container_type="50ml耐热瓶", liquid_volume_ml=2.0,
        )
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.station_code, "UV_Vis_Spectrometer_V1")
        self.assertEqual(result.required_capabilities, ("uv-vis",))
        self.assertIsNone(result.minimum_liquid_volume_ml)
        self.assertEqual(result.reason_codes, ())

    def test_ambiguous_or_unmapped_observation_abstains(self) -> None:
        both = resolve_observation_protocol("XRD and UV-Vis")
        self.assertEqual(both.status, "ambiguous")
        self.assertIn("observation_capability_ambiguous", both.reason_codes)
        unknown = resolve_observation_protocol("unmapped hyper-spectroscopy")
        self.assertEqual(unknown.status, "unsupported")
        self.assertIn("observation_capability_unmapped", unknown.reason_codes)

    def test_stale_skill_evidence_cannot_issue_verified_protocol(self) -> None:
        root = Path(__file__).resolve().parents[1]
        index = json.loads((root / "chem_resources" / "workstation_capability_index.json").read_text(encoding="utf-8"))
        xrd = next(item for item in index["workstations"] if item["station_code"] == "XRD_V1")
        xrd["experiment_capabilities"][0]["evidence"]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.json"
            path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
            result = resolve_observation_protocol(
                "XRD", capability_index_path=path,
                sample_state="suspension", container_type="进样瓶",
                liquid_volume_ml=4.0,
            )
        self.assertEqual(result.status, "unsupported")
        self.assertIn("observation_skill_source_stale", result.reason_codes)

    def test_reviewed_capability_binding_must_agree_with_operation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        index = json.loads((root / "chem_resources" / "workstation_capability_index.json").read_text(encoding="utf-8"))
        xrd = next(item for item in index["workstations"] if item["station_code"] == "XRD_V1")
        xrd["capability_operation_bindings"]["xrd"] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.json"
            path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
            result = resolve_observation_protocol("XRD", capability_index_path=path)
        self.assertEqual(result.status, "unsupported")
        self.assertIn("observation_capability_binding_mismatch", result.reason_codes)


if __name__ == "__main__":
    unittest.main()
