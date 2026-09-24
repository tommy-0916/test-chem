from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_skills.llm_retry import (
    RetryableGatewayError,
    call_with_gateway_retry,
    is_retryable_gateway_error,
    is_terminal_gateway_error,
    safe_gateway_error_code,
)


class GatewayRetryClassificationTests(unittest.TestCase):
    def test_safe_error_code_rejects_provider_controlled_token(self) -> None:
        error = RuntimeError("sanitized")
        error.body = {"error": {"code": "sk-kimi-PRIVATE_CANARY"}}

        self.assertIsNone(safe_gateway_error_code(error))

    def test_quota_403_is_terminal_even_when_body_says_rate_limit(self) -> None:
        error = RuntimeError("sanitized")
        error.responses_diagnostics = {
            "http_status": 403,
            "error": {"type": "access_terminated_error", "code": "rate_limit_error"},
        }

        self.assertFalse(is_retryable_gateway_error(error))

    def test_responses_upstream_error_is_retryable_without_http_failure(self) -> None:
        error = RuntimeError("sanitized")
        error.responses_diagnostics = {
            "http_status": 200,
            "error": {"type": "upstream_error"},
        }

        self.assertTrue(is_retryable_gateway_error(error))

    def test_incomplete_terminal_is_not_retried_even_with_upstream_code(self) -> None:
        error = RuntimeError("sanitized")
        error.responses_diagnostics = {
            "event_type": "response.incomplete",
            "response_status": "incomplete",
            "http_status": 200,
            "error": {"code": "upstream_error"},
        }

        self.assertFalse(is_retryable_gateway_error(error))

    def test_inner_403_outranks_outer_upstream_message(self) -> None:
        inner = RuntimeError("private provider text")
        inner.status_code = 403
        outer = RuntimeError("upstream_error")
        outer.__cause__ = inner
        attempts = 0

        def operation() -> None:
            nonlocal attempts
            attempts += 1
            raise outer

        with self.assertRaises(RuntimeError), patch(
            "agent_skills.llm_retry.time.sleep"
        ) as sleep:
            call_with_gateway_retry(
                operation, max_retries=8, wall_timeout_seconds=30
            )
        self.assertEqual(attempts, 1)
        sleep.assert_not_called()

    def test_terminal_403_is_not_replayed(self) -> None:
        attempts = 0

        def operation() -> None:
            nonlocal attempts
            attempts += 1
            error = RuntimeError("sanitized")
            error.status_code = 403
            raise error

        with self.assertRaises(RuntimeError), patch("agent_skills.llm_retry.time.sleep") as sleep:
            call_with_gateway_retry(operation, max_retries=8, wall_timeout_seconds=30)

        self.assertEqual(attempts, 1)
        sleep.assert_not_called()

    def test_chat_429_insufficient_quota_body_is_not_replayed(self) -> None:
        attempts = 0

        def operation() -> None:
            nonlocal attempts
            attempts += 1
            error = RuntimeError("provider message must not drive classification")
            error.status_code = 429
            error.body = {
                "error": {
                    "message": "untrusted provider detail",
                    "type": "rate_limit_error",
                    "code": "insufficient_quota",
                }
            }
            raise error

        with self.assertRaises(RuntimeError), patch(
            "agent_skills.llm_retry.time.sleep"
        ) as sleep:
            call_with_gateway_retry(
                operation, max_retries=8, wall_timeout_seconds=30
            )

        self.assertEqual(attempts, 1)
        sleep.assert_not_called()

    def test_direct_chat_body_error_code_is_classified(self) -> None:
        error = RuntimeError("sanitized")
        error.status_code = 429
        error.body = {"code": "insufficient_quota", "message": "ignored"}

        self.assertTrue(is_terminal_gateway_error(error))
        self.assertFalse(is_retryable_gateway_error(error))

    def test_transient_failure_uses_one_shared_retry_loop(self) -> None:
        attempts = 0

        def operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RetryableGatewayError("upstream_error")
            return "ok"

        with patch("agent_skills.llm_retry.record_retry_sleep", create=True), patch(
            "agent_skills.llm_retry.time.sleep", return_value=None
        ):
            result = call_with_gateway_retry(
                operation,
                max_retries=1,
                wall_timeout_seconds=30,
                sleep=lambda _seconds: None,
            )

        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()
