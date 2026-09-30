"""Route trust is deployment input, separate from Research event constraints."""

from __future__ import annotations

import json
import base64
from hashlib import sha256
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reaserch_agent import run_research_agent as cli


class RouteTrustCliTest(unittest.TestCase):
    def _deployment_config(self, root: Path):
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            self.skipTest("cryptography unavailable")
        from reaserch_agent.route_attestation import TrustedAcquisitionEventV1
        from reaserch_agent.route_signed_event import (
            SIGNED_EVENT_SCHEMA_VERSION_V1,
            signed_trusted_acquisition_event_message_v1,
        )
        kb = root / "kb"
        kb.mkdir()
        pdf = kb / "_pdf_sources" / "campaign" / "source.pdf"
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b"PDF fixture for configuration signature validation")
        key = Ed25519PrivateKey.generate()
        issuer, key_id = "test-acquisition-service", "test-source-key"
        event = TrustedAcquisitionEventV1(
            schema_version="trusted_acquisition_event_v1",
            paper_id="paper-test", kb_relative_path="_pdf_sources/campaign/source.pdf",
            document_digest="sha256_" + sha256(pdf.read_bytes()).hexdigest(),
            document_kind="primary_paper", attestation_digest="sha256_" + "a" * 64,
            issuer=issuer, identity_verdict="primary_verified",
        ).model_dump(mode="json")
        envelope = {
            "schema_version": SIGNED_EVENT_SCHEMA_VERSION_V1,
            "key_id": key_id, "event": event,
            "signature": base64.b64encode(key.sign(
                signed_trusted_acquisition_event_message_v1(key_id=key_id, event=event),
            )).decode("ascii"),
        }
        raw = {
            "schema_version": "route-trust-config/v1",
            "signed_route_source_events": [envelope],
            "trusted_route_public_keys": {key_id: {
                "public_key_base64": base64.b64encode(key.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw,
                )).decode("ascii"),
                "allowed_issuer": issuer,
            }},
            "signed_route_signature_reviews": [],
            "trusted_route_signature_public_keys": {},
            "trusted_route_capabilities_by_group": [],
            "trusted_route_group_roles_by_group": [],
        }
        path = root / "deployment.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path, kb, raw

    def test_absent_configuration_stays_conservative(self):
        self.assertEqual(cli.load_route_trust_config(None, None), {})
        args = cli.build_parser().parse_args(["--query", "test"])
        self.assertIsNone(args.route_trust_config)

    def test_loader_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trust.json"
            kb = Path(directory) / "kb"
            kb.mkdir()
            path.write_text('{"schema_version":"route-trust-config/v1",'
                            '"schema_version":"route-trust-config/v1"}', encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "duplicate JSON key"):
                cli.load_route_trust_config(str(path), str(kb))

    def test_loader_rejects_config_inside_knowledge_base(self):
        with tempfile.TemporaryDirectory() as directory:
            kb = Path(directory) / "kb"
            kb.mkdir()
            path = kb / "trust.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "outside the knowledge base"):
                cli.load_route_trust_config(str(path), str(kb))

    def test_loader_rejects_missing_signed_event_key_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trust.json"
            kb = Path(directory) / "kb"
            kb.mkdir()
            path.write_text(json.dumps({
                "schema_version": "route-trust-config/v1",
                "signed_route_source_events": [],
                "trusted_route_public_keys": {},
                "signed_route_signature_reviews": [],
                "trusted_route_signature_public_keys": {},
                "trusted_route_capabilities_by_group": [],
                "trusted_route_group_roles_by_group": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "requires signed source events"):
                cli.load_route_trust_config(str(path), str(kb))

    def test_legacy_config_and_optional_relative_supply_register_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path, kb, raw = self._deployment_config(Path(directory))
            legacy = cli.load_route_trust_config(str(path), str(kb))
            self.assertNotIn("trusted_inventory_register_paths", legacy)
            spec = kb / "supplies.json"
            spec.write_text(json.dumps({"schema": "candidate_supply_spec/v1", "items": []}),
                            encoding="utf-8")
            raw["trusted_inventory_register_paths"] = {
                "candidate_supply_spec/v1": "kb/supplies.json",
            }
            path.write_text(json.dumps(raw), encoding="utf-8")
            loaded = cli.load_route_trust_config(str(path), str(kb))
            self.assertEqual(loaded["trusted_inventory_register_paths"], {
                "candidate_supply_spec/v1": str(spec.resolve()),
            })
            raw["trusted_inventory_register_paths"]["candidate_supply_spec/v1"] = str(spec)
            path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(
                cli.load_route_trust_config(str(path), str(kb))
                ["trusted_inventory_register_paths"],
                loaded["trusted_inventory_register_paths"],
            )

    def test_inventory_register_config_cannot_replace_required_or_add_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path, kb, raw = self._deployment_config(Path(directory))
            for changed in (
                {key: value for key, value in raw.items()
                 if key != "signed_route_source_events"},
                {**raw, "model_inventory_paths": {}},
            ):
                with self.subTest(fields=list(changed)):
                    changed["trusted_inventory_register_paths"] = {}
                    path.write_text(json.dumps(changed), encoding="utf-8")
                    with self.assertRaisesRegex(SystemExit, "required v1 trust fields"):
                        cli.load_route_trust_config(str(path), str(kb))

    def test_inventory_register_rejects_wrong_mapping_or_register(self):
        with tempfile.TemporaryDirectory() as directory:
            path, kb, raw = self._deployment_config(Path(directory))
            for value in (None, [], "kb/specs.json", {"material-inventory/v1": "kb/x.json"},
                          {"unknown/v1": "kb/x.json"}):
                with self.subTest(value=value):
                    raw["trusted_inventory_register_paths"] = value
                    path.write_text(json.dumps(raw), encoding="utf-8")
                    with self.assertRaisesRegex(SystemExit, "only candidate_supply_spec/v1"):
                        cli.load_route_trust_config(str(path), str(kb))

    def test_inventory_register_rejects_nonlocal_or_invalid_path_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path, kb, raw = self._deployment_config(Path(directory))
            for name in (None, 7, "", " ", " kb/specs.json", "https://example.test/spec.json",
                         "file:///supplies.json", "\\\\server\\share\\specs.json"):
                with self.subTest(name=name):
                    raw["trusted_inventory_register_paths"] = {"candidate_supply_spec/v1": name}
                    path.write_text(json.dumps(raw), encoding="utf-8")
                    with self.assertRaisesRegex(SystemExit, "non-empty local file paths"):
                        cli.load_route_trust_config(str(path), str(kb))

    def test_inventory_register_rejects_missing_directory_and_outside_kb_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path, kb, raw = self._deployment_config(Path(directory))
            outside = Path(directory) / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            for name in ("kb/missing.json", "kb", "outside.json", str(outside)):
                with self.subTest(name=name):
                    raw["trusted_inventory_register_paths"] = {"candidate_supply_spec/v1": name}
                    path.write_text(json.dumps(raw), encoding="utf-8")
                    with self.assertRaisesRegex(SystemExit, "existing file inside the knowledge base"):
                        cli.load_route_trust_config(str(path), str(kb))

    def test_main_passes_only_independently_loaded_trust_to_agent(self):
        deployment_trust = {
            "signed_route_source_events": (object(),),
            "trusted_route_public_keys": {"source-key": object()},
            "signed_route_signature_reviews": (object(),),
            "trusted_route_signature_public_keys": {"review-key": object()},
            "trusted_route_capabilities_by_group": {("p", "g", "d"): ("mix",)},
            "trusted_route_group_roles_by_group": {("p", "g", "d"): "synthesis"},
            "trusted_inventory_register_paths": {"candidate_supply_spec/v1": "trusted-specs.json"},
        }
        fake_state = SimpleNamespace(status="completed", campaign_id="", plan_revisions=[])
        args = [
            "run_research_agent.py", "--query", "test", "--disable-llm",
            "--no-online-literature", "--no-web-search", "--no-ledger",
            "--device-context-json", "{}", "--route-trust-config", "deployment.json",
            "--constraints-json", json.dumps({
                "route_decision_goal_v1": {"goal_id": "G"},
                "trusted_inventory_register_paths": {"candidate_supply_spec/v1": "model-specs.json"},
            }),
        ]
        with patch.object(sys, "argv", args), patch.object(
            cli, "load_route_trust_config", return_value=deployment_trust,
        ) as load, patch.object(cli, "ResearchAgent") as agent_type, patch.object(
            cli, "print_summary"
        ), patch.object(cli, "write_debug_log", return_value=Path("debug.json")):
            agent_type.return_value.run.return_value = fake_state
            self.assertEqual(cli.main(), 0)

        load.assert_called_once_with("deployment.json", None)
        kwargs = agent_type.call_args.kwargs
        for name, expected in deployment_trust.items():
            self.assertIs(kwargs[name], expected)
        self.assertNotIn("route_trust_config", agent_type.return_value.run.call_args.kwargs["constraints"])

    def test_v1_cannot_silently_ignore_route_trust_config(self):
        args = [
            "run_research_agent.py", "--query", "test", "--contract-version", "v1",
            "--route-trust-config", "deployment.json", "--device-context-json", "{}",
        ]
        with patch.object(sys, "argv", args):
            with self.assertRaisesRegex(SystemExit, "requires --contract-version v2"):
                cli.main()


if __name__ == "__main__":
    unittest.main()
