"""Core helpers for the research agent."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional

from agent_skills.llm_retry import (
    call_with_gateway_retry,
    safe_gateway_error_code,
    safe_gateway_error_metadata,
)
from agent_skills.llm_timing import measure_llm_request
from agent_skills.responses_diagnostics import (
    format_responses_failure,
    get_responses_diagnostics,
)

logger = logging.getLogger(__name__)


DEFAULT_LLM_MAX_RETRIES = 8


class BaseAgent:
    """Small base class with optional LLM invocation helpers."""

    def __init__(
        self, model: Any = None, max_retries: int = DEFAULT_LLM_MAX_RETRIES
    ) -> None:
        self._model = model
        retry_override = os.getenv("REFINER_LLM_MAX_RETRIES")
        if retry_override:
            try:
                max_retries = max(0, int(retry_override))
            except ValueError:
                pass
        self._model_handles_transport_retries = (
            getattr(model, "handles_transport_retries", False) is True
        )
        if self._model_handles_transport_retries:
            retry_budget = getattr(
                model,
                "_chem_gateway_max_retries",
                getattr(model, "_transport_max_retries", 0),
            )
        else:
            retry_budget = getattr(model, "_chem_gateway_max_retries", max_retries)
        if not isinstance(retry_budget, (int, float, str)):
            retry_budget = max_retries
        try:
            self._gateway_retry_budget = max(0, int(retry_budget or 0))
        except (TypeError, ValueError):
            self._gateway_retry_budget = max(0, int(max_retries))
        # Retain this private attribute for callers/tests that introspect the
        # historical name. BaseAgent now makes exactly one logical model call;
        # either the model or the shared middleware owns transport retries.
        self._max_retries = 1
        try:
            self._format_retry_budget = max(
                0, int(os.getenv("RESEARCH_JSON_FORMAT_MAX_RETRIES", "2"))
            )
        except ValueError:
            self._format_retry_budget = 2

    @property
    def has_model(self) -> bool:
        return self._model is not None

    def _model_request_is_timed(self) -> bool:
        """Return whether the concrete model boundary owns request timing.

        Runnable bindings keep the provider model under ``bound``.  Inspect
        both objects so wrapping an already-instrumented provider does not
        count the same wire request twice.
        """

        bound = getattr(self._model, "bound", None)
        return any(
            getattr(candidate, "handles_request_timing", False) is True
            for candidate in (self._model, bound)
            if candidate is not None
        )

    def _timing_model_name(self) -> str:
        """Return a metadata-only model label without serializing the client."""

        for attribute in ("model_name", "model", "_model"):
            value = getattr(self._model, attribute, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return type(self._model).__name__

    def _build_messages(self, system_prompt: str, task_prompt: str) -> List[Any]:
        """Build messages in a format supported by common chat model SDKs."""
        try:
            from langchain_core.messages import HumanMessage, SystemMessage

            if os.getenv("REFINER_LLM_SINGLE_USER_MESSAGE") == "1":
                return [
                    HumanMessage(
                        content=(
                            f"[System instructions]\n{system_prompt}\n\n"
                            f"[Task]\n{task_prompt}"
                        )
                    )
                ]

            return [
                SystemMessage(content=system_prompt),
                HumanMessage(content=task_prompt),
            ]
        except Exception:
            if os.getenv("REFINER_LLM_SINGLE_USER_MESSAGE") == "1":
                return [
                    {
                        "role": "user",
                        "content": (
                            f"[System instructions]\n{system_prompt}\n\n"
                            f"[Task]\n{task_prompt}"
                        ),
                    }
                ]

            return [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task_prompt},
            ]

    def _normalize_response_content(self, response: Any) -> str:
        if response is None:
            raise ValueError("LLM returned None response")

        content = getattr(response, "content", response)
        if isinstance(content, str):
            if not content.strip():
                raise ValueError("LLM returned empty content")
            return content

        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        parts.append(text)
            normalized = "\n".join(part for part in parts if part)
            if normalized.strip():
                return normalized

        raise ValueError(f"Unsupported LLM response content: {type(content).__name__}")

    def invoke_text(
        self, system_prompt: str, task_prompt: str, *,
        on_llm_failure: Callable[[Exception, int], None] | None = None,
    ) -> str:
        if not self.has_model:
            raise RuntimeError("No LLM model is configured for this agent")

        messages = self._build_messages(system_prompt, task_prompt)
        application_attempt = 0

        def invoke_model() -> Any:
            nonlocal application_attempt
            application_attempt += 1
            try:
                if self._model_request_is_timed():
                    return self._model.invoke(messages)
                with measure_llm_request(
                    component=os.getenv("CHEM_LLM_COMPONENT", "research"),
                    model=self._timing_model_name(),
                    transport="research_chat",
                ):
                    return self._model.invoke(messages)
            except Exception as exc:
                if on_llm_failure is not None:
                    on_llm_failure(exc, application_attempt)
                raise

        last_error: Optional[Exception] = None
        try:
            if self._model_handles_transport_retries:
                # Responses/pool adapters already use call_with_gateway_retry.
                # Replaying them here would multiply the same logical call.
                response = invoke_model()
            else:
                wall_timeout = getattr(
                    self._model, "_chem_wall_timeout_seconds", None
                )
                if not isinstance(wall_timeout, (int, float)) or isinstance(
                    wall_timeout, bool
                ):
                    wall_timeout = None
                response = call_with_gateway_retry(
                    invoke_model,
                    max_retries=self._gateway_retry_budget,
                    wall_timeout_seconds=wall_timeout,
                    logger=logger,
                    operation_name="Research chat request",
                )
        except Exception as exc:
            last_error = exc

        if last_error is not None:
            owner = "model" if self._model_handles_transport_retries else "shared"
            safe_failure = format_responses_failure(last_error)
            if safe_failure is None:
                error_type, http_status = safe_gateway_error_metadata(last_error)
                error_code = safe_gateway_error_code(last_error)
                fields = [f"type={error_type}"]
                if http_status is not None:
                    fields.append(f"status={http_status}")
                if error_code is not None:
                    fields.append(f"code={error_code}")
                safe_failure = "Gateway failure: " + ", ".join(fields)
            error = RuntimeError(
                "LLM request failed "
                f"(retry_owner={owner}, max_retries={self._gateway_retry_budget}): "
                f"{safe_failure}"
            )
            diagnostics = get_responses_diagnostics(last_error)
            if diagnostics is not None:
                error.responses_diagnostics = diagnostics
            # Preserve classification metadata while suppressing a raw
            # provider body/URL from displayed tracebacks and log records.
            error.__context__ = last_error
            raise error from None

        # Empty/malformed content is an application output problem, not a
        # transport problem. invoke_json may repair it with a new prompt.
        return self._normalize_response_content(response)

    def invoke_json(
        self, system_prompt: str, task_prompt: str, *,
        on_llm_failure: Callable[[Exception, int], None] | None = None,
    ) -> Dict[str, Any]:
        last_format_error: Optional[ValueError] = None
        for format_attempt in range(self._format_retry_budget + 1):
            repair_prompt = task_prompt
            if format_attempt:
                repair_prompt += (
                    "\n\nSerialization repair only: return exactly one complete JSON "
                    "object with no Markdown or commentary. Preserve the task's "
                    "scientific requirements."
                )
            try:
                raw_text = self.invoke_text(
                    system_prompt,
                    repair_prompt,
                    on_llm_failure=on_llm_failure,
                )
                parsed = self._parse_json_response(raw_text)
                if not isinstance(parsed, dict):
                    raise ValueError(
                        "Expected JSON object response, got: "
                        f"{type(parsed).__name__}"
                    )
                return parsed
            except ValueError as exc:
                last_format_error = exc
                if format_attempt >= self._format_retry_budget:
                    break
                logger.warning(
                    "Research JSON serialization failed (%s/%s); requesting "
                    "serialization-only repair",
                    format_attempt + 1,
                    self._format_retry_budget + 1,
                )

        raise ValueError(
            "Research JSON serialization failed after "
            f"{self._format_retry_budget} format retries: {last_format_error}"
        ) from last_format_error

    def _parse_json_response(self, raw_text: str) -> Any:
        candidates = [raw_text.strip()]

        fenced_matches = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", raw_text)
        candidates.extend(match.strip() for match in fenced_matches if match.strip())

        object_match = re.search(r"(\{[\s\S]*\})", raw_text)
        if object_match:
            candidates.append(object_match.group(1).strip())

        array_match = re.search(r"(\[[\s\S]*\])", raw_text)
        if array_match:
            candidates.append(array_match.group(1).strip())

        for candidate in candidates:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

        raise ValueError(f"Failed to parse JSON from LLM response: {raw_text}")
