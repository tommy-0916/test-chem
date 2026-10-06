"""Tests for the programmatic manual-supply channel (G3 ingestion round).

The wrong-file negatives use the real Du-2022 primary article and SI bytes:
both Wiley documents share the same DOI surface, so only the document-kind
furniture check can tell them apart.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from reaserch_agent.route_manual_supply import (
    ManualSupplyRequestV1,
    manual_supply_request_digest,
    receive_manual_supply,
    reenter_ingestion,
    write_manual_supply_request,
)

_REPO = Path(__file__).resolve().parent.parent
_G3_KB = (
    _REPO / "result" / "g3-du2022-real-input-20261006" / "kb"
    / "_pdf_sources" / "du2022"
)
DU_SI = _G3_KB / (
    "9f05be9f4a5d5c607dbefdeb8ebb5c89ac5fb7ee4385534e174c88e921efc7ee.pdf"
)
DU_MAIN = _G3_KB / (
    "87552f9c65ef308a7e2dda502d9078bba5396fe716fff659a71ae962de51206b.pdf"
)
DU_DOI = "10.1002/anie.202209350"
DU_SI_NAME = "anie202209350-sup-0001-misc_information.pdf"
DU_SI_URL = (
    "https://onlinelibrary.wiley.com/action/downloadSupplement"
    "?doi=10.1002%2Fanie.202209350&file=anie202209350-sup-0001-misc_information.pdf"
)


def _si_request() -> ManualSupplyRequestV1:
    return ManualSupplyRequestV1(
        schema_version="manual_supply_request_v1",
        paper_id="du-2022",
        document_kind="supporting_information",
        title="High-Entropy Prussian Blue Analogues and Their Oxide Family",
        parent_doi=DU_DOI,
        expected_file_name=DU_SI_NAME,
        verified_url=DU_SI_URL,
        failure_reason="http_403",
    )


def _main_request() -> ManualSupplyRequestV1:
    return ManualSupplyRequestV1(
        schema_version="manual_supply_request_v1",
        paper_id="du-2022",
        document_kind="primary_paper",
        title="High-Entropy Prussian Blue Analogues and Their Oxide Family",
        doi=DU_DOI,
        expected_file_name="anie202209350.pdf",
        verified_url="https://onlinelibrary.wiley.com/doi/pdf/10.1002/anie.202209350",
        failure_reason="http_403",
    )


class RequestValidationTest(unittest.TestCase):
    def test_si_requires_parent_doi(self) -> None:
        with self.assertRaises(ValueError):
            ManualSupplyRequestV1(
                schema_version="manual_supply_request_v1",
                paper_id="p", document_kind="supporting_information",
                expected_file_name="si.pdf",
                verified_url="https://example.org/si.pdf",
                failure_reason="http_403",
            )

    def test_primary_rejects_parent_doi(self) -> None:
        with self.assertRaises(ValueError):
            ManualSupplyRequestV1(
                schema_version="manual_supply_request_v1",
                paper_id="p", document_kind="primary_paper",
                doi=DU_DOI, parent_doi=DU_DOI,
                expected_file_name="a.pdf",
                verified_url="https://example.org/a.pdf",
                failure_reason="http_403",
            )

    def test_verified_url_must_be_https(self) -> None:
        with self.assertRaises(ValidationError):
            ManualSupplyRequestV1(
                schema_version="manual_supply_request_v1",
                paper_id="p", document_kind="primary_paper", doi=DU_DOI,
                expected_file_name="a.pdf",
                verified_url="http://example.org/a.pdf",
                failure_reason="http_403",
            )

    def test_request_fields_cover_display_contract(self) -> None:
        request = _si_request()
        self.assertEqual(request.document_kind, "supporting_information")
        self.assertEqual(request.parent_doi, DU_DOI)
        self.assertEqual(request.expected_file_name, DU_SI_NAME)
        self.assertEqual(request.verified_url, DU_SI_URL)
        self.assertEqual(request.failure_reason, "http_403")
        self.assertTrue(request.title)

    def test_request_persistence_round_trip(self) -> None:
        request = _si_request()
        with tempfile.TemporaryDirectory() as tmp:
            path = write_manual_supply_request(tmp, request)
            payload = json.loads(path.read_text(encoding="utf-8"))
            loaded = ManualSupplyRequestV1.model_validate(payload, strict=True)
            self.assertEqual(
                manual_supply_request_digest(loaded),
                manual_supply_request_digest(request),
            )
            self.assertIn("registry/manual_supply_v1/requests",
                          path.as_posix())


class ReceiveGuardTest(unittest.TestCase):
    def test_missing_file_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = receive_manual_supply(
                tmp, _si_request(), Path(tmp) / "nope.pdf")
            self.assertEqual(record.stage, "receive_rejected")
            self.assertEqual(record.detail, "supplied_file_unavailable")

    def test_non_pdf_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "notes.txt"
            bad.write_bytes(b"not a pdf")
            record = receive_manual_supply(tmp, _si_request(), bad)
            self.assertEqual(record.stage, "receive_rejected")
            self.assertEqual(record.detail, "supplied_file_not_pdf")

    def test_oversize_pdf_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.pdf"
            with big.open("wb") as handle:
                handle.write(b"%PDF-1.7\n")
                handle.truncate(16 * 1024 * 1024 + 1)
            record = receive_manual_supply(tmp, _si_request(), big)
            self.assertEqual(record.stage, "receive_rejected")
            self.assertEqual(record.detail, "supplied_file_too_large")

    def test_reentry_requires_verified_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "notes.txt"
            bad.write_bytes(b"not a pdf")
            record = receive_manual_supply(tmp, _si_request(), bad)
            with self.assertRaises(ValueError):
                reenter_ingestion(tmp, _si_request(), record)


@unittest.skipUnless(DU_SI.is_file() and DU_MAIN.is_file(),
                     "Du 2022 PDF fixtures missing")
class RealDocumentIdentityTest(unittest.TestCase):
    def _records(self, root: str, request: ManualSupplyRequestV1) -> list:
        paper_key_digest = manual_supply_request_digest(request)
        directory = Path(root) / "registry" / "manual_supply_v1" / "records"
        files = list(directory.glob(f"*_{paper_key_digest.removeprefix('sha256_')}.jsonl"))
        self.assertEqual(len(files), 1)
        return [json.loads(line)
                for line in files[0].read_text(encoding="utf-8").splitlines()]

    def test_correct_si_unlocks_ingestion_three_stages(self) -> None:
        request = _si_request()
        with tempfile.TemporaryDirectory() as tmp:
            received = receive_manual_supply(tmp, request, DU_SI)
            self.assertEqual(received.stage, "identity_verified")
            checks = {c.name: c.outcome for c in received.checks}
            self.assertEqual(checks["kind_marker_present"], "pass")
            # The Wiley SI omits the parent DOI; absence is corroborating.
            self.assertEqual(checks["doi_evidence"], "absent")
            ingested = reenter_ingestion(tmp, request, received)
            self.assertEqual(ingested.stage, "ingested")
            self.assertEqual(len(ingested.ingested_groups), 12)
            self.assertEqual(ingested.ingested_diagnostics, ())
            # The file landed in the existing _pdf_sources upload layout.
            self.assertTrue(
                (Path(tmp) / received.supplied_kb_path).is_file())
            self.assertTrue(received.supplied_kb_path.startswith(
                "_pdf_sources/du-2022/"))
            stages = [r["stage"] for r in self._records(tmp, request)]
            self.assertEqual(stages,
                             ["received", "identity_verified", "ingested"])

    def test_main_article_handed_as_si_rejected(self) -> None:
        request = _si_request()
        with tempfile.TemporaryDirectory() as tmp:
            record = receive_manual_supply(tmp, request, DU_MAIN)
            self.assertEqual(record.stage, "identity_rejected")
            checks = {c.name: c.outcome for c in record.checks}
            # Same DOI surface as the SI: only the kind marker separates them.
            self.assertEqual(checks["doi_evidence"], "pass")
            self.assertEqual(checks["kind_marker_present"], "fail")
            with self.assertRaises(ValueError):
                reenter_ingestion(tmp, request, record)
            stages = [r["stage"] for r in self._records(tmp, request)]
            self.assertEqual(stages, ["received", "identity_rejected"])

    def test_si_handed_as_primary_rejected(self) -> None:
        request = _main_request()
        with tempfile.TemporaryDirectory() as tmp:
            record = receive_manual_supply(tmp, request, DU_SI)
            self.assertEqual(record.stage, "identity_rejected")
            checks = {c.name: c.outcome for c in record.checks}
            self.assertEqual(checks["kind_marker_absent"], "fail")
            self.assertEqual(checks["doi_evidence"], "fail")

    def test_main_article_as_primary_unlocks(self) -> None:
        request = _main_request()
        with tempfile.TemporaryDirectory() as tmp:
            received = receive_manual_supply(tmp, request, DU_MAIN)
            self.assertEqual(received.stage, "identity_verified")
            ingested = reenter_ingestion(tmp, request, received)
            # The primary article has no experimental section heading; an
            # honest zero-group result is recorded, not a silent pass.
            self.assertEqual(ingested.stage, "ingestion_failed")
            self.assertEqual(ingested.ingested_groups, ())


if __name__ == "__main__":
    unittest.main()
