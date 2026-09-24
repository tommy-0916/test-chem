"""Retry ownership tests for the Research Agent base class."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from reaserch_agent.core import BaseAgent


class _RetryableError(RuntimeError):
    status_code = 502


class _ChatModelWithExternalRetry:
    _chem_gateway_max_retries = 1

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        if self.calls == 1:
            raise _RetryableError("bad gateway")
        return SimpleNamespace(content="done")


class _TransportRetryModel:
    handles_transport_retries = True

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        raise RuntimeError("adapter exhausted its own retry budget")


def test_chat_model_uses_shared_ten_second_retry_without_outer_multiplication():
    model = _ChatModelWithExternalRetry()
    agent = BaseAgent(model=model, max_retries=8)

    with patch("agent_skills.llm_retry.time.sleep") as sleep:
        result = agent.invoke_text("system", "task")

    assert result == "done"
    assert model.calls == 2
    assert agent._max_retries == 1
    sleep.assert_called_once_with(10.0)


def test_transport_retry_model_is_not_retried_again_by_base_agent():
    model = _TransportRetryModel()
    agent = BaseAgent(model=model, max_retries=8)

    with pytest.raises(
        RuntimeError,
        match=r"retry_owner=model, max_retries=0",
    ):
        agent.invoke_text("system", "task")

    assert model.calls == 1
