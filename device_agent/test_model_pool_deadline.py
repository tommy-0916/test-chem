"""Offline tests for one shared wall deadline across model-pool failover."""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_skills.llm_retry as llm_retry
from agent_skills.llm_retry import LogicalCallDeadlineExceeded, logical_deadline
from utils.llm_factory import ModelPoolChatModel


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TextBackend:
    def __init__(
        self,
        name: str,
        clock: FakeClock,
        duration: float,
        result=None,
        error=None,
        *,
        base_url: str = "https://provider.invalid/v1",
        api_key: str = "shared-key",
    ) -> None:
        self.name = name
        self.timeout = 10.0
        self.base_url = base_url
        self.api_key = api_key
        self.clock = clock
        self.duration = duration
        self.result = result
        self.error = error
        self.deadlines: list[float] = []
        self.remaining_at_start: list[float] = []
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        deadline = logical_deadline(self.timeout)
        self.deadlines.append(deadline)
        self.remaining_at_start.append(deadline - self.clock.monotonic())
        self.clock.advance(self.duration)
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise ConnectionError(f"{self.name} unavailable")
        return self.result


class NativeBackend:
    def __init__(
        self,
        name: str,
        bound,
        *,
        retries: int = 0,
        base_url: str = "https://provider.invalid/v1",
        api_key: str = "shared-key",
    ) -> None:
        self.name = name
        self.timeout = 10.0
        self.bound = bound
        self._transport_max_retries = retries
        self.base_url = base_url
        self.api_key = api_key

    def bind_tools(self, tools, **kwargs):
        return self.bound


class ModelPoolDeadlineTests(unittest.TestCase):
    def test_pool_rejects_non_finite_wall_timeout(self):
        clock = FakeClock()
        backend = TextBackend("only", clock, duration=0.0, result=object())
        for value in (0.0, float("inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ModelPoolChatModel([backend], wall_timeout_seconds=value)

    def test_text_failover_reuses_one_absolute_deadline(self):
        clock = FakeClock()
        answer = SimpleNamespace(content="ok")
        first = TextBackend("first", clock, duration=6.0)
        second = TextBackend("second", clock, duration=0.0, result=answer)
        pool = ModelPoolChatModel(
            [first, second], max_rounds=1, wall_timeout_seconds=10.0
        )

        with patch.dict(os.environ, {"REFINER_LLM_WALL_TIMEOUT_SECONDS": "10"}), patch.object(
            llm_retry.time, "monotonic", side_effect=clock.monotonic
        ):
            self.assertIs(pool.invoke([{"role": "user", "content": "test"}]), answer)

        self.assertEqual(first.deadlines, [105.0])
        self.assertEqual(second.deadlines, [110.0])
        self.assertEqual(first.remaining_at_start, [5.0])
        self.assertEqual(second.remaining_at_start, [4.0])

    def test_terminal_error_can_fail_over_once_to_distinct_identity(self):
        class ForbiddenError(RuntimeError):
            status_code = 403

        clock = FakeClock()
        answer = SimpleNamespace(content="ok")
        first = TextBackend(
            "first",
            clock,
            duration=6.0,
            error=ForbiddenError("forbidden"),
            base_url="https://provider-a.invalid/v1",
            api_key="key-a",
        )
        second = TextBackend(
            "second",
            clock,
            duration=0.0,
            result=answer,
            base_url="https://provider-b.invalid/v1",
            api_key="key-b",
        )
        pool = ModelPoolChatModel(
            [first, second], max_rounds=8, wall_timeout_seconds=10.0
        )

        with patch.dict(os.environ, {"REFINER_LLM_WALL_TIMEOUT_SECONDS": "10"}), patch.object(
            llm_retry.time, "monotonic", side_effect=clock.monotonic
        ):
            self.assertIs(pool.invoke([]), answer)

        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)
        self.assertEqual(first.deadlines, [105.0])
        self.assertEqual(second.deadlines, [110.0])
        self.assertEqual(first.remaining_at_start, [5.0])
        self.assertEqual(second.remaining_at_start, [4.0])

    def test_terminal_error_does_not_retry_same_endpoint_and_key(self):
        class ForbiddenError(RuntimeError):
            status_code = 403

        clock = FakeClock()
        first = TextBackend(
            "first", clock, duration=0.0,
            error=ForbiddenError("PRIVATE_PROVIDER_BODY"),
        )
        duplicate = TextBackend(
            "duplicate", clock, duration=0.0, result=SimpleNamespace(content="bad")
        )
        pool = ModelPoolChatModel(
            [first, duplicate], max_rounds=8, wall_timeout_seconds=10.0
        )

        with patch.dict(os.environ, {"REFINER_LLM_WALL_TIMEOUT_SECONDS": "10"}), patch.object(
            llm_retry.time, "monotonic", side_effect=clock.monotonic
        ):
            with self.assertRaises(RuntimeError) as caught:
                pool.invoke([])

        self.assertNotIn("PRIVATE_PROVIDER_BODY", str(caught.exception))
        self.assertTrue(llm_retry.is_terminal_gateway_error(caught.exception))

        self.assertEqual(first.calls, 1)
        self.assertEqual(duplicate.calls, 0)

    def test_native_pool_refuses_another_backend_after_shared_budget(self):
        clock = FakeClock()
        first_bound = Mock()
        second_bound = Mock()

        def exhaust_budget(messages, **kwargs):
            clock.advance(10.0)
            raise ConnectionError("first unavailable")

        first_bound.invoke.side_effect = exhaust_budget
        second_bound.invoke.return_value = SimpleNamespace(content="must not run")
        pool = ModelPoolChatModel(
            [
                NativeBackend("first", first_bound),
                NativeBackend("second", second_bound),
            ],
            max_rounds=1,
            wall_timeout_seconds=10.0,
        )

        with patch.dict(os.environ, {"REFINER_LLM_WALL_TIMEOUT_SECONDS": "10"}), patch.object(
            llm_retry.time, "monotonic", side_effect=clock.monotonic
        ):
            with self.assertRaises(LogicalCallDeadlineExceeded):
                pool.bind_tools([]).invoke([])

        self.assertEqual(clock.now, 110.0)
        first_bound.invoke.assert_called_once()
        second_bound.invoke.assert_not_called()

    def test_native_pool_preempts_blocked_backend_and_uses_reserved_failover_slice(self):
        blocked = threading.Event()
        first_bound = Mock()
        second_bound = Mock()
        first_bound.invoke.side_effect = lambda messages, **kwargs: blocked.wait(5.0)
        answer = SimpleNamespace(content="healthy failover")
        second_bound.invoke.return_value = answer
        pool = ModelPoolChatModel(
            [
                NativeBackend("first", first_bound),
                NativeBackend("second", second_bound),
            ],
            max_rounds=1,
            wall_timeout_seconds=0.05,
        )

        started = time.monotonic()
        with patch.dict(os.environ, {"REFINER_LLM_WALL_TIMEOUT_SECONDS": "0.05"}):
            self.assertIs(pool.bind_tools([]).invoke([]), answer)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5)
        first_bound.invoke.assert_called_once()
        second_bound.invoke.assert_called_once()

    def test_native_pool_retries_transient_child_inside_shared_deadline(self):
        clock = FakeClock()
        bound = Mock()
        answer = SimpleNamespace(content="ok")
        bound.invoke.side_effect = [TimeoutError("PRIVATE_TIMEOUT_BODY"), answer]
        pool = ModelPoolChatModel(
            [NativeBackend("first", bound, retries=1)],
            max_rounds=8,
            wall_timeout_seconds=20.0,
        )

        with patch.dict(
            os.environ, {"REFINER_LLM_WALL_TIMEOUT_SECONDS": "20"}
        ), patch.object(
            llm_retry.time, "monotonic", side_effect=clock.monotonic
        ), patch.object(
            llm_retry.time, "sleep", side_effect=clock.advance
        ):
            self.assertIs(pool.bind_tools([]).invoke([]), answer)

        self.assertEqual(bound.invoke.call_count, 2)
        self.assertEqual(clock.now, 110.0)

    def test_native_pool_quota_error_skips_duplicate_credential(self):
        class QuotaError(RuntimeError):
            status_code = 429
            body = {
                "error": {
                    "code": "insufficient_quota",
                    "message": "PRIVATE_QUOTA_BODY",
                }
            }

        first_bound = Mock()
        duplicate_bound = Mock()
        first_bound.invoke.side_effect = QuotaError("PRIVATE_QUOTA_BODY")
        duplicate_bound.invoke.return_value = SimpleNamespace(content="bad")
        pool = ModelPoolChatModel(
            [
                NativeBackend("first", first_bound, retries=3),
                NativeBackend("duplicate", duplicate_bound, retries=3),
            ],
            max_rounds=8,
            wall_timeout_seconds=20.0,
        )

        with self.assertRaises(RuntimeError) as caught:
            pool.bind_tools([]).invoke([])

        self.assertNotIn("PRIVATE_QUOTA_BODY", str(caught.exception))
        self.assertTrue(llm_retry.is_terminal_gateway_error(caught.exception))
        first_bound.invoke.assert_called_once()
        duplicate_bound.invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
