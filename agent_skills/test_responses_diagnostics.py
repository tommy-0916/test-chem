"""Offline contract and adversarial tests for redacted Responses diagnostics."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_skills.responses_diagnostics import (
    attach_responses_diagnostics,
    build_response_diagnostics,
    format_responses_failure,
    get_responses_diagnostics,
)


class ResponsesDiagnosticsTests(unittest.TestCase):
    def test_mapping_extracts_only_supported_fields(self):
        response = {
            "id": "resp_123", "status": "failed", "model": "example-model",
            "_request_id": "req_123", "max_output_tokens": 32768,
            "error": {"code": "server_error", "message": "Provider failed", "type": "server", "param": "model", "headers": {"secret": "never"}},
            "usage": {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42,
                      "output_tokens_details": {"reasoning_tokens": 7},
                      "input_tokens_details": {"cached_tokens": 10}},
            "input": "PRIVATE_PROMPT", "output": "PRIVATE_OUTPUT", "tools": "PRIVATE_TOOLS", "headers": "PRIVATE_HEADERS",
        }
        result = build_response_diagnostics(response=response, event={"type": "response.failed"}, http_status=200, phase="device_plan")
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["error"], {"code": "server_error", "message": "Provider failed", "type": "server", "param": "model"})
        self.assertEqual(result["usage"], {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42, "reasoning_tokens": 7, "cached_tokens": 10})
        self.assertEqual(result["request_id"], "req_123")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("headers", json.dumps(result))

    def test_sdk_style_objects_need_no_model_dump(self):
        response = Mock(id="resp_mock", status="incomplete", model="example", max_output_tokens=99)
        response.error = None
        response._request_id = "req_mock"
        response.incomplete_details = SimpleNamespace(reason="max_output_tokens")
        response.usage = SimpleNamespace(input_tokens=2, output_tokens=4, total_tokens=6)
        response.model_dump.side_effect = AssertionError("Must not serialize response")
        result = build_response_diagnostics(event=SimpleNamespace(type="response.incomplete", response=response))
        self.assertEqual(result["incomplete_reason"], "max_output_tokens")
        self.assertEqual(result["response_id"], "resp_mock")
        response.model_dump.assert_not_called()

    def test_direct_error_event_does_not_invent_error_type(self):
        result = build_response_diagnostics(event={"type": "error", "code": "quota", "message": "No quota", "param": None, "request_id": "r1"})
        self.assertEqual(result["error"], {"code": "quota", "message": "No quota"})
        self.assertEqual(result["request_id"], "r1")
        self.assertNotIn("response_status", result)

    def test_nested_event_error(self):
        result = build_response_diagnostics(event={"type": "response.error", "error": {"code": "bad", "message": "failed", "type": "gateway"}})
        self.assertEqual(result["error"]["type"], "gateway")

    def test_missing_detail_remains_missing(self):
        self.assertEqual(build_response_diagnostics(), {"schema_version": 1})
        self.assertEqual(build_response_diagnostics(event={"type": "response.failed"}), {"schema_version": 1, "event_type": "response.failed"})

    def test_malformed_event_type_is_not_coerced_or_hashed(self):
        self.assertEqual(build_response_diagnostics(event={"type": {"private": "data"}}), {"schema_version": 1})

    def test_explicit_request_context_takes_precedence(self):
        result = build_response_diagnostics(response={"request_id": "old", "model": "old", "max_output_tokens": 1}, request_id="explicit", model="new", max_output_tokens=2, phase="read", http_status=524)
        self.assertEqual(result["request_id"], "explicit")
        self.assertEqual(result["model"], "new")
        self.assertEqual(result["max_output_tokens"], 2)
        self.assertEqual(result["http_status"], 524)

    def test_redacts_bearer_sk_assignment_and_multiline(self):
        message = 'Bearer bearer-secret\nAPI_KEY="quoted secret"; token=token-secret; password=pass-secret\nauth: auth-secret sk-provider-secret'
        result = build_response_diagnostics(response={"error": {"message": message}})
        sanitized = result["error"]["message"]
        for secret in ("bearer-secret", "quoted secret", "token-secret", "pass-secret", "auth-secret", "sk-provider-secret"):
            self.assertNotIn(secret, sanitized)
        self.assertNotIn("\n", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_url_credentials_query_and_fragment_redacted(self):
        message = "Gateway https://user:password@example.test/v1/responses?api_key=query-secret&foo=other-secret#fragment-secret failed"
        result = build_response_diagnostics(response={"error": {"message": message}})
        sanitized = result["error"]["message"]
        for secret in ("user:password", "query-secret", "other-secret", "fragment-secret"):
            self.assertNotIn(secret, sanitized)
        self.assertIn("example.test/v1/responses", sanitized)

    def test_explicit_nonstandard_secret_redacted_before_truncation(self):
        secret = "credential-with-no-provider-prefix" + "z" * 2000
        result = build_response_diagnostics(response={"error": {"message": "prefix " + secret + " after"}}, api_key=secret)
        self.assertEqual(result["error"]["message"], "prefix [REDACTED] after")

    def test_all_string_fields_redacted_and_bounded(self):
        bad = "sk-another-secret " + "x" * 3000
        result = build_response_diagnostics(response={"id": bad, "status": bad, "error": {"code": bad, "message": bad, "type": bad, "param": bad}, "incomplete_details": {"reason": bad}}, event={"type": bad}, request_id=bad, model=bad, phase=bad)
        for key, value in result.items():
            if isinstance(value, str):
                self.assertLessEqual(len(value), 256, key)
            if isinstance(value, dict):
                for error_key, error_value in value.items():
                    self.assertLessEqual(len(error_value), 1000 if error_key == "message" else 256)
        self.assertNotIn("sk-another-secret", json.dumps(result))

    def test_malformed_and_negative_usage_not_coerced(self):
        result = build_response_diagnostics(response={"usage": {"input_tokens": True, "output_tokens": -1, "total_tokens": "20", "output_tokens_details": {"reasoning_tokens": 1.5}, "input_tokens_details": {"cached_tokens": 10**100}}}, max_output_tokens=False, http_status=999)
        self.assertNotIn("usage", result)
        self.assertNotIn("max_output_tokens", result)
        self.assertNotIn("http_status", result)

    def test_zero_counts_are_retained_without_inferred_totals(self):
        result = build_response_diagnostics(response={"usage": {"input_tokens": 0, "output_tokens": 0}})
        self.assertEqual(result["usage"], {"input_tokens": 0, "output_tokens": 0})

    def test_arbitrary_objects_are_never_stringified(self):
        class Forbidden:
            def __str__(self):
                raise AssertionError("Must not stringify")
        result = build_response_diagnostics(response={"id": Forbidden(), "error": {"message": Forbidden()}, "usage": Forbidden()}, phase=Forbidden())
        self.assertEqual(result, {"schema_version": 1})

    def test_properties_that_raise_do_not_replace_failure(self):
        class BrokenResponse:
            @property
            def error(self):
                raise RuntimeError("property failed")
        self.assertEqual(build_response_diagnostics(response=BrokenResponse()), {"schema_version": 1})

    def test_private_sdk_fields_are_never_accessed(self):
        class GuardedResponse:
            status = "failed"

            def __getattribute__(self, name):
                if name in {"input", "output", "tools", "headers", "model_dump", "__dict__"}:
                    raise AssertionError("Private payload access")
                return object.__getattribute__(self, name)

        result = build_response_diagnostics(response=GuardedResponse())
        self.assertEqual(result, {"schema_version": 1, "response_status": "failed"})

    def test_no_environment_credentials_are_read(self):
        with patch("os.getenv", side_effect=AssertionError("Must not inspect environment")):
            self.assertEqual(build_response_diagnostics(), {"schema_version": 1})

    def test_attachment_and_reads_are_isolated_allowlist_copies(self):
        source = {"response_id": "r1", "error": {"message": "safe"}, "headers": {"key": "secret"}, "payload": "PRIVATE"}
        exc = RuntimeError("Original failure")
        attach_responses_diagnostics(exc, source)
        source["error"]["message"] = "mutated"
        first = get_responses_diagnostics(exc)
        self.assertEqual(first["error"]["message"], "safe")
        first["error"]["message"] = "again"
        self.assertEqual(get_responses_diagnostics(exc)["error"]["message"], "safe")
        self.assertNotIn("PRIVATE", format_responses_failure(exc))
        self.assertIs(type(exc), RuntimeError)
        self.assertEqual(str(exc), "Original failure")

    def test_externally_attached_data_is_sanitized_again(self):
        exc = RuntimeError()
        exc.responses_diagnostics = {"schema_version": 999, "error": {"message": "Bearer secret"}, "headers": "PRIVATE"}
        result = get_responses_diagnostics(exc)
        self.assertEqual(result["schema_version"], 1)
        self.assertNotIn("secret", result["error"]["message"])
        self.assertNotIn("headers", result)

    def test_redaction_is_stable_across_attachment_and_readback(self):
        diagnostic = build_response_diagnostics(response={"error": {"message": 'api_key="top secret"; token=another-secret'}})
        exc = RuntimeError()
        attach_responses_diagnostics(exc, diagnostic)
        self.assertEqual(get_responses_diagnostics(exc), diagnostic)

    def test_cause_and_context_are_both_searched(self):
        outer, cause, context = RuntimeError(), RuntimeError(), RuntimeError()
        outer.__cause__ = cause
        outer.__context__ = context
        attach_responses_diagnostics(context, {"response_id": "context"})
        self.assertEqual(get_responses_diagnostics(outer)["response_id"], "context")

    def test_chain_limit_and_cycles(self):
        chain = [RuntimeError() for _ in range(9)]
        for left, right in zip(chain, chain[1:]):
            left.__cause__ = right
        attach_responses_diagnostics(chain[-1], {"response_id": "too-deep"})
        self.assertIsNone(get_responses_diagnostics(chain[0]))
        attach_responses_diagnostics(chain[-2], {"response_id": "eighth"})
        self.assertEqual(get_responses_diagnostics(chain[0])["response_id"], "eighth")
        a, b = RuntimeError(), RuntimeError()
        a.__cause__, b.__context__ = b, a
        self.assertIsNone(get_responses_diagnostics(a))

    def test_broken_exception_attributes_do_not_hide_original_failure(self):
        class BrokenException(RuntimeError):
            def __getattribute__(self, name):
                if name in {"responses_diagnostics", "__cause__", "__context__"}:
                    raise RuntimeError("attribute failed")
                return super().__getattribute__(name)

        self.assertIsNone(get_responses_diagnostics(BrokenException("original")))

    def test_immutable_exception_attachment_is_best_effort(self):
        class ImmutableException(RuntimeError):
            def __setattr__(self, name, value):
                raise AttributeError("immutable")

        exc = ImmutableException("original")
        attach_responses_diagnostics(exc, {"response_id": "r1"})
        self.assertIsNone(get_responses_diagnostics(exc))
        self.assertEqual(str(exc), "original")

    def test_formatter_never_includes_arbitrary_exception_text(self):
        exc = RuntimeError("PROMPT_OR_CREDENTIAL_SHOULD_NOT_BE_PRINTED")
        self.assertIsNone(format_responses_failure(exc))
        attach_responses_diagnostics(exc, {"event_type": "response.failed", "error": {"message": "safe\n\x1b[31m message"}})
        formatted = format_responses_failure(exc)
        self.assertNotIn("PROMPT_OR_CREDENTIAL", formatted)
        self.assertNotIn("\n", formatted)
        self.assertNotIn("\x1b", formatted)

    def test_formatter_has_global_bound(self):
        exc = RuntimeError()
        attach_responses_diagnostics(exc, {**{name: "x" * 2000 for name in ("event_type", "response_id", "request_id", "response_status", "incomplete_reason", "model", "phase")}, "error": {name: "y" * 2000 for name in ("code", "message", "type", "param")}})
        self.assertLessEqual(len(format_responses_failure(exc)), 4096)


if __name__ == "__main__":
    unittest.main()
