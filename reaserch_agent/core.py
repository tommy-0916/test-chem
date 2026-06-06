"""Core helpers for the research agent."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class BaseAgent:
    """Small base class with optional LLM invocation helpers."""

    def __init__(self, model: Any = None, max_retries: int = 4) -> None:
        self._model = model
        retry_override = os.getenv("REFINER_LLM_MAX_RETRIES")
        if retry_override:
            try:
                max_retries = max(1, int(retry_override))
            except ValueError:
                pass
        self._max_retries = max_retries

    @property
    def has_model(self) -> bool:
        return self._model is not None

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

    def invoke_text(self, system_prompt: str, task_prompt: str) -> str:
        if not self.has_model:
            raise RuntimeError("No LLM model is configured for this agent")

        messages = self._build_messages(system_prompt, task_prompt)
        wait_schedule = [1, 2, 4, 8]
        last_error: Optional[Exception] = None

        for attempt in range(self._max_retries):
            try:
                response = self._model.invoke(messages)
                return self._normalize_response_content(response)
            except Exception as exc:  # pragma: no cover - depends on remote model
                last_error = exc
                logger.warning(
                    "LLM invocation failed (%s/%s): %s",
                    attempt + 1,
                    self._max_retries,
                    exc,
                )
                if attempt < self._max_retries - 1:
                    time.sleep(wait_schedule[min(attempt, len(wait_schedule) - 1)])

        raise RuntimeError(f"LLM call failed after {self._max_retries} retries: {last_error}")

    def invoke_json(self, system_prompt: str, task_prompt: str) -> Dict[str, Any]:
        raw_text = self.invoke_text(system_prompt, task_prompt)
        parsed = self._parse_json_response(raw_text)
        if not isinstance(parsed, dict):
            raise ValueError(f"Expected JSON object response, got: {type(parsed).__name__}")
        return parsed

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
