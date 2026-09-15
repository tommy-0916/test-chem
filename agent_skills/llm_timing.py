"""Prompt-free, durable request lifecycle telemetry for the gateway boundary."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .llm_retry import AttemptContext, LogicalCallDeadlineExceeded, current_attempt
from .responses_diagnostics import safe_response_event_type

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_event(event: dict[str, object]) -> None:
    destination = os.getenv("CHEM_LLM_TIMING_JSONL", "").strip()
    if not destination:
        return
    try:
        path = Path(destination).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("timing append made no progress")
                offset += written
        finally:
            os.close(descriptor)
    except Exception as exc:
        logger.error("CHEM_LLM_TIMING_WRITE_FAILED error_type=%s status=%s attempt_id=%s",
                     type(exc).__name__, event.get("status"), event.get("attempt_id"))


class RequestTiming:
    def __init__(self, component: str, model: str, transport: str) -> None:
        attempt = current_attempt()
        self.attempt = attempt
        self.started = time.monotonic()
        self.last_emitted = self.started
        self.last_event_type = "request.started"
        self.last_event_at = _now()
        self.done = False
        self.lock = threading.Lock()
        self.base: dict[str, object] = {
            "pid": os.getpid(), "component": component, "model": model,
            "transport": transport, "attempt_id": attempt.attempt_id if attempt else uuid.uuid4().hex,
            "logical_call_id": attempt.logical_call_id if attempt else uuid.uuid4().hex,
            "deadline_monotonic": attempt.deadline if attempt else None,
            "started_at": self.last_event_at,
        }
        self.emit("started")
        if attempt is not None:
            attempt.on_timeout.append(self.fail)
            if attempt.cancelled or time.monotonic() >= attempt.deadline:
                error = LogicalCallDeadlineExceeded("LLM attempt was cancelled before starting its request")
                self.fail(error)
                raise error

    def emit(self, status: str, error_type: str = "") -> None:
        elapsed = round(time.monotonic() - self.started, 6)
        _append_event({**self.base, "timestamp": _now(), "status": status,
                       "elapsed_seconds": elapsed,
                       "counted_llm_seconds": elapsed if status == "success" else 0.0,
                       "last_event_type": self.last_event_type,
                       "last_event_at": self.last_event_at, "error_type": error_type})

    def observe(self, event_type: str) -> None:
        with self.lock:
            if self.done:
                return
            event_type = safe_response_event_type(event_type)
            changed = self.last_event_type != event_type
            self.last_event_type = event_type
            self.last_event_at = _now()
            # Persist transitions and a heartbeat instead of writing per token.
            if changed or time.monotonic() - self.last_emitted >= 5:
                self.emit("event")
                self.last_emitted = time.monotonic()

    def finish(self, error: BaseException | None = None) -> None:
        with self.lock:
            if self.done:
                return
            self.done = True
            if error is None and self.attempt and (self.attempt.cancelled or time.monotonic() >= self.attempt.deadline):
                error = LogicalCallDeadlineExceeded("LLM wall deadline exceeded")
            self.emit("failed" if error else "success", type(error).__name__ if error else "")

    def fail(self, error: BaseException) -> None:
        self.finish(error)


@contextmanager
def measure_llm_request(*, component: str, model: str, transport: str) -> Iterator[RequestTiming]:
    timing = RequestTiming(component, model, transport)
    try:
        yield timing
    except BaseException as exc:
        timing.finish(exc)
        raise
    else:
        timing.finish()


def record_retry_sleep(seconds: float, attempt: AttemptContext, *, started: bool = False) -> None:
    _append_event({"timestamp": _now(), "pid": os.getpid(), "status": "retry_sleep_started" if started else "retry_sleep",
                   "attempt_id": attempt.attempt_id, "logical_call_id": attempt.logical_call_id,
                   "deadline_monotonic": attempt.deadline,
                   "scheduled_sleep_seconds" if started else "sleep_seconds": round(seconds, 6),
                   "elapsed_seconds": 0.0, "counted_llm_seconds": 0.0})
