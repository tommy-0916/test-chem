"""Opt-in timing for underlying LLM requests.

Set ``CHEM_LLM_TIMING_JSONL`` to append one event per transport request. The
event contains no prompt, response, URL, or credentials. Failed requests are
recorded separately so an end-to-end benchmark can subtract API timeout stalls
without discarding successful model latency.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def _append_event(event: dict[str, object]) -> None:
    destination = os.getenv("CHEM_LLM_TIMING_JSONL", "").strip()
    if not destination:
        return
    try:
        path = Path(destination).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
    except Exception:
        # Benchmark telemetry must never change agent behavior.
        return


@contextmanager
def measure_llm_request(
    *, component: str, model: str, transport: str
) -> Iterator[None]:
    """Record one transport request, counting only successful calls."""

    started = time.perf_counter()
    try:
        yield
    except BaseException as exc:
        elapsed = time.perf_counter() - started
        _append_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "pid": os.getpid(),
                "component": component,
                "model": model,
                "transport": transport,
                "status": "failed",
                "elapsed_seconds": round(elapsed, 6),
                "counted_llm_seconds": 0.0,
                "error_type": type(exc).__name__,
            }
        )
        raise
    else:
        elapsed = time.perf_counter() - started
        _append_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "pid": os.getpid(),
                "component": component,
                "model": model,
                "transport": transport,
                "status": "success",
                "elapsed_seconds": round(elapsed, 6),
                "counted_llm_seconds": round(elapsed, 6),
                "error_type": "",
            }
        )
