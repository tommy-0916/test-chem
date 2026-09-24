"""Execution-boundary adapters between device packages and the (real) lab.

The real dispatch chain stays blocked in this repository (see the
lab-operation skill's dispatch_guard). RealExecutionAdapter therefore
refuses to run; use mock / manual / listen adapters to close the loop
until a human explicitly authorizes and implements real dispatch.
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional


class ExecutionBlockedError(RuntimeError):
    """Raised when an adapter refuses to dispatch to the real laboratory."""


class BaseExecutionAdapter:
    """Turn a successful device package into one observation dict."""

    name = "base"
    # Whether executing sends the workflow toward a real laboratory.  The
    # campaign runner's scientific-review gate blocks unapproved
    # review-flagged workflows on any adapter that crosses this boundary.
    # Safe default: True (only pure simulation opts out).
    real_lab_boundary = True

    def execute(
        self,
        package: Dict[str, Any],
        iteration_dir: Path,
    ) -> Dict[str, Any]:
        raise NotImplementedError


class MockExecutionAdapter(BaseExecutionAdapter):
    """Deterministic simulated execution results for dry runs and tests.

    ``observation_file`` may contain a JSON object (returned every time)
    or a JSON array (consumed per successful execution, last one repeats).
    """

    name = "mock"
    real_lab_boundary = False

    def __init__(self, observation_file: str | Path | None = None) -> None:
        self._observations: List[Dict[str, Any]] = []
        self._single: Optional[Dict[str, Any]] = None
        self._calls = 0
        if observation_file:
            data = json.loads(
                Path(observation_file).expanduser().read_text(encoding="utf-8")
            )
            if isinstance(data, list):
                self._observations = [dict(item) for item in data if isinstance(item, dict)]
            elif isinstance(data, dict):
                self._single = dict(data)

    def execute(
        self,
        package: Dict[str, Any],
        iteration_dir: Path,
    ) -> Dict[str, Any]:
        self._calls += 1
        if self._observations:
            index = min(self._calls - 1, len(self._observations) - 1)
            return dict(self._observations[index])
        if self._single is not None:
            return dict(self._single)
        workflow_json = package.get("workflow_json")
        steps = workflow_json.get("steps") if isinstance(workflow_json, dict) else []
        step_count = len(steps) if isinstance(steps, list) else 0
        return {
            "summary": (
                f"[mock 执行] workflow 共 {step_count} 步已按计划执行完成，"
                "未出现异常，目标 observation point 数据已回传。"
            ),
            "status": "success",
            "source": "mock_execution_adapter",
        }


class ManualExecutionAdapter(BaseExecutionAdapter):
    """Wait for a human/external system to drop observation_in.json."""

    name = "manual"

    def __init__(
        self,
        timeout_seconds: float = 3600.0,
        poll_seconds: float = 2.0,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds

    def execute(
        self,
        package: Dict[str, Any],
        iteration_dir: Path,
    ) -> Dict[str, Any]:
        inbox = iteration_dir / "observation_in.json"
        instructions = iteration_dir / "AWAITING_OBSERVATION.md"
        instructions.write_text(
            "# 等待实验结果回传\n\n"
            "本轮 device workflow 已生成（见同目录 device_package.json）。\n"
            "真实下发链路在本仓库被安全阻断；请在机器执行完成后，把实验结果 JSON 写入：\n\n"
            f"    {inbox}\n\n"
            '格式示例：{"summary": "XRD 显示目标相纯相", "metrics": {...}}\n',
            encoding="utf-8",
        )
        print(
            f"[manual-adapter] waiting for observation file: {inbox}",
            flush=True,
        )
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if inbox.exists():
                try:
                    data = json.loads(inbox.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    time.sleep(self.poll_seconds)
                    continue
                if isinstance(data, dict):
                    return data
            time.sleep(self.poll_seconds)
        raise TimeoutError(
            f"manual execution adapter timed out waiting for {inbox}"
        )


class _ObservationHTTPHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path.rstrip("/") not in {"", "/observation"}:
            self._respond(404, {"error": "POST /observation expected"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._respond(400, {"error": "body must be a JSON object"})
            return
        if not isinstance(data, dict):
            self._respond(400, {"error": "body must be a JSON object"})
            return
        self.server.received_observation = data  # type: ignore[attr-defined]
        self._respond(200, {"status": "received"})

    def _respond(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # keep orchestrator stdout clean


class ListenExecutionAdapter(BaseExecutionAdapter):
    """Receive the machine's result via one HTTP POST /observation."""

    name = "listen"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8899,
        timeout_seconds: float = 3600.0,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_seconds = timeout_seconds
        self.bound_port: Optional[int] = None

    def execute(
        self,
        package: Dict[str, Any],
        iteration_dir: Path,
    ) -> Dict[str, Any]:
        server = HTTPServer((self.host, self.port), _ObservationHTTPHandler)
        server.timeout = 1.0
        server.received_observation = None  # type: ignore[attr-defined]
        self.bound_port = int(server.server_address[1])
        print(
            "[listen-adapter] waiting for experiment result: "
            f"POST http://{self.host}:{self.bound_port}/observation",
            flush=True,
        )
        deadline = time.monotonic() + self.timeout_seconds
        try:
            while time.monotonic() < deadline:
                server.handle_request()
                observation = getattr(server, "received_observation", None)
                if isinstance(observation, dict):
                    inbox = iteration_dir / "observation_in.json"
                    inbox.write_text(
                        json.dumps(observation, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    return observation
            raise TimeoutError(
                "listen execution adapter timed out waiting for POST /observation"
            )
        finally:
            server.server_close()


class RealExecutionAdapter(BaseExecutionAdapter):
    """Real lab dispatch is intentionally blocked in this repository."""

    name = "real"

    def execute(
        self,
        package: Dict[str, Any],
        iteration_dir: Path,
    ) -> Dict[str, Any]:
        raise ExecutionBlockedError(
            "真实实验下发链路在本仓库被安全阻断（lab-operation dispatch_guard）。"
            "接通真实执行需要人工授权并显式实现 RealExecutionAdapter；"
            "当前请使用 manual 或 listen 适配器接收真实实验室回传的结果。"
        )


def build_adapter(
    name: str,
    *,
    mock_observation_file: str | Path | None = None,
    listen_host: str = "127.0.0.1",
    listen_port: int = 8899,
    timeout_seconds: float = 3600.0,
) -> BaseExecutionAdapter:
    normalized = (name or "").strip().lower()
    if normalized == "mock":
        return MockExecutionAdapter(observation_file=mock_observation_file)
    if normalized == "manual":
        return ManualExecutionAdapter(timeout_seconds=timeout_seconds)
    if normalized == "listen":
        return ListenExecutionAdapter(
            host=listen_host,
            port=listen_port,
            timeout_seconds=timeout_seconds,
        )
    if normalized == "real":
        return RealExecutionAdapter()
    raise ValueError(f"unknown execution adapter: {name}")
