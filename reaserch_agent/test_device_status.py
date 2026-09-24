"""Unit tests for live device-availability overlays on both layers (P5)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reaserch_agent.tools.device_context import (
    apply_device_status,
    ensure_device_context,
    load_device_status,
)

SAMPLE_CONTEXT = {
    "source": "test",
    "planning_policy": "base policy.",
    "workstations": [
        {"station_name": "Drying_Oven_V1", "display_name": "烘干机"},
        {"station_name": "Centrifuge_V1", "display_name": "离心机"},
        {"station_name": "Magnetic_Stirring_V1", "display_name": "磁力搅拌工作站"},
    ],
}


class DeviceStatusResearchSideTest(unittest.TestCase):
    def test_load_device_status_accepts_both_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flat = Path(tmp) / "flat.json"
            flat.write_text(
                json.dumps({"烘干机": "offline"}, ensure_ascii=False),
                encoding="utf-8",
            )
            nested = Path(tmp) / "nested.json"
            nested.write_text(
                json.dumps(
                    {"stations": {"Centrifuge_V1": "busy"}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertEqual(load_device_status(flat), {"烘干机": "offline"})
            self.assertEqual(load_device_status(nested), {"Centrifuge_V1": "busy"})

    def test_apply_device_status_flags_stations(self) -> None:
        updated = apply_device_status(
            SAMPLE_CONTEXT,
            {"烘干机": "offline", "Centrifuge_V1": "busy"},
        )

        by_name = {ws["station_name"]: ws for ws in updated["workstations"]}
        self.assertEqual(by_name["Drying_Oven_V1"]["availability"], "offline")
        self.assertIn("⛔", by_name["Drying_Oven_V1"]["availability_note"])
        self.assertEqual(by_name["Centrifuge_V1"]["availability"], "busy")
        self.assertIn("⚠️", by_name["Centrifuge_V1"]["availability_note"])
        self.assertNotIn("availability", by_name["Magnetic_Stirring_V1"])
        self.assertIn("烘干机", updated["planning_policy"])
        self.assertIn("不可用", updated["planning_policy"])
        # original context untouched
        self.assertNotIn("availability", SAMPLE_CONTEXT["workstations"][0])

    def test_ensure_device_context_applies_status_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "status.json"
            status_path.write_text(
                json.dumps({"烘干机": "维修"}, ensure_ascii=False),
                encoding="utf-8",
            )
            constraints = {
                "device_context": json.loads(json.dumps(SAMPLE_CONTEXT)),
                "device_status_path": str(status_path),
            }

            resolved = ensure_device_context(constraints)

            context = resolved["device_context"]
            self.assertEqual(context["station_status"], {"烘干机": "维修"})
            dryer = next(
                ws
                for ws in context["workstations"]
                if ws["station_name"] == "Drying_Oven_V1"
            )
            self.assertIn("⛔", dryer["availability_note"])


class DeviceStatusLoaderSideTest(unittest.TestCase):
    """Device-agent WorkstationLoader must surface ⛔ banners in prompts."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        status_path = Path(self._tmp.name) / "status.json"
        status_path.write_text(
            json.dumps(
                {
                    "stations": {
                        "dual_electrochemical": "offline",
                        "磁力搅拌工作站": "busy",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._previous_env = os.environ.get("CHEM_DEVICE_STATUS_JSON")
        os.environ["CHEM_DEVICE_STATUS_JSON"] = str(status_path)
        device_agent_dir = REPO_ROOT / "device_agent"
        if str(device_agent_dir) not in sys.path:
            sys.path.insert(0, str(device_agent_dir))

    def tearDown(self) -> None:
        if self._previous_env is None:
            os.environ.pop("CHEM_DEVICE_STATUS_JSON", None)
        else:
            os.environ["CHEM_DEVICE_STATUS_JSON"] = self._previous_env
        self._tmp.cleanup()

    def test_old_format_prompt_flags_offline_station(self) -> None:
        from device_agent.utils.workstation_loader import WorkstationLoader

        loader = WorkstationLoader(use_new_format=False)
        prompt = loader.format_for_prompt()

        self.assertIn("⛔ 当前不可用（status=offline）", prompt)
        # banner sits inside the dual_electrochemical section
        section = prompt.split("双工位电化学工作站", 1)[1][:400]
        self.assertIn("⛔", section)
        # busy station flagged as waiting, not blocked
        self.assertIn("⚠️ 当前占用（status=busy）", prompt)

    def test_station_status_matching_by_code_and_display_name(self) -> None:
        from device_agent.utils.workstation_loader import WorkstationLoader

        loader = WorkstationLoader(use_new_format=False)
        self.assertEqual(
            loader._station_status("dual_electrochemical", {}),
            "offline",
        )
        self.assertEqual(
            loader._station_status(
                "any_code",
                {"display_name": "磁力搅拌工作站"},
            ),
            "busy",
        )
        self.assertEqual(loader._station_status("unknown", {}), "")


if __name__ == "__main__":
    unittest.main()
