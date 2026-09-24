"""Offline regressions for local contract errors versus transport failures."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from agent_skills.native_tools import NativeToolConfigurationError
from chem_agent_contracts.container_requirements import (
    LogicalContainerContractError,
    parse_logical_container_requirements,
)
from chem_agent_contracts.v2 import LogicalContainerV2
from reaserch_agent.workflow import ResearchAgent


def invalid_lid_error() -> ValidationError:
    """Reproduce the rejected XRD carrier field, without loading run secrets."""
    try:
        LogicalContainerV2(
            logical_container_id="A02-XRD-CARRIER",
            container_type="XRD基底片",
            lid_state="not_applicable",
        )
    except ValidationError as error:
        return error
    raise AssertionError("The V2 contract must reject an unsupported lid enum")


class ContractFailureClassificationTests(unittest.TestCase):
    def test_shared_container_contract_error_and_wrappers_are_quality_failures(self):
        with self.assertRaises(LogicalContainerContractError) as caught:
            parse_logical_container_requirements(
                {"macro_step_id": "MS_008", "container_requirements": [{
                    "logical_container_id": "A02-XRD-CARRIER",
                    "lid_state": "not_applicable",
                }]},
                sequence=8,
            )
        original = caught.exception
        self.assertEqual(original.issues[0].field_path,
                         "/macro_plan/7/container_requirements/0/lid_state")
        self.assertEqual(ResearchAgent._classify_failure(original), "macro_quality_error")
        for relationship in ("__cause__", "__context__"):
            with self.subTest(relationship=relationship):
                wrapped = RuntimeError("Safe publication failure")
                setattr(wrapped, relationship, original)
                self.assertEqual(ResearchAgent._classify_failure(wrapped), "macro_quality_error")

    def test_pydantic_help_url_does_not_make_local_contract_error_a_network_failure(self):
        error = invalid_lid_error()
        self.assertIn("https://errors.pydantic.dev/", str(error))
        self.assertEqual(error.errors()[0]["loc"], ("lid_state",))
        self.assertEqual(error.errors()[0]["input"], "not_applicable")
        self.assertEqual(ResearchAgent._classify_failure(error), "macro_quality_error")

    def test_local_contract_error_survives_explicit_cause_wrapper(self):
        error = invalid_lid_error()
        wrapped = RuntimeError("Research contract publication failed")
        wrapped.__cause__ = error
        self.assertEqual(ResearchAgent._classify_failure(wrapped), "macro_quality_error")
        self.assertIs(wrapped.__cause__, error)

    def test_local_contract_error_survives_implicit_context_wrapper(self):
        error = invalid_lid_error()
        wrapped = RuntimeError("Research contract publication failed")
        wrapped.__context__ = error
        self.assertEqual(ResearchAgent._classify_failure(wrapped), "macro_quality_error")
        self.assertIs(wrapped.__context__, error)

    def test_suppressed_traceback_context_still_identifies_local_contract_error(self):
        original = invalid_lid_error()
        try:
            try:
                raise original
            except ValidationError:
                raise RuntimeError("Safe publication failure") from None
        except RuntimeError as error:
            self.assertTrue(error.__suppress_context__)
            self.assertIs(error.__context__, original)
            self.assertEqual(ResearchAgent._classify_failure(error), "macro_quality_error")

    def test_exception_chain_cycle_does_not_hide_contract_context(self):
        wrapped = RuntimeError("Safe outer failure")
        inner = RuntimeError("Safe inner failure")
        original = invalid_lid_error()
        wrapped.__cause__ = inner
        inner.__cause__ = wrapped
        inner.__context__ = original
        self.assertEqual(ResearchAgent._classify_failure(wrapped), "macro_quality_error")

    def test_http_200_response_failed_remains_generation_error(self):
        error = RuntimeError("response.failed with http_status and URL metadata")
        error.responses_diagnostics = {
            "phase": "stream_event",
            "http_status": 200,
            "event_type": "response.failed",
            "response_status": "failed",
        }
        self.assertEqual(ResearchAgent._classify_failure(error), "macro_generation_error")

    def test_http_rejections_remain_network_failures_through_wrappers(self):
        for status in (400, 429, 500):
            for relationship in ("__cause__", "__context__"):
                with self.subTest(status=status, relationship=relationship):
                    error = RuntimeError("Provider rejected request")
                    error.responses_diagnostics = {"phase": "request", "http_status": status}
                    wrapped = RuntimeError("Safe request failure")
                    setattr(wrapped, relationship, error)
                    self.assertEqual(
                        ResearchAgent._classify_failure(wrapped), "network_or_retrieval_error"
                    )

    def test_actual_timeout_with_http_200_remains_network_failure(self):
        class ReadTimeout(Exception):
            pass

        for relationship in ("__cause__", "__context__"):
            with self.subTest(relationship=relationship):
                error = ReadTimeout("Stream stopped delivering events")
                error.responses_diagnostics = {"phase": "stream_read", "http_status": 200}
                wrapped = RuntimeError("Safe stream failure")
                setattr(wrapped, relationship, error)
                self.assertEqual(
                    ResearchAgent._classify_failure(wrapped), "network_or_retrieval_error"
                )

    def test_legacy_network_text_classification_is_preserved(self):
        for message in ("survey LLM failed: connection timed out", "retrieval network timeout"):
            with self.subTest(message=message):
                self.assertEqual(
                    ResearchAgent._classify_failure(message), "network_or_retrieval_error"
                )

    def test_non_contract_configuration_and_generation_errors_are_unchanged(self):
        self.assertEqual(
            ResearchAgent._classify_failure(NativeToolConfigurationError("Invalid native runtime")),
            "configuration_error",
        )
        self.assertEqual(
            ResearchAgent._classify_failure(ValueError("Could not parse JSON from response")),
            "macro_generation_error",
        )


if __name__ == "__main__":
    unittest.main()
