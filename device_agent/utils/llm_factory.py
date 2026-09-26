"""
LLM instance factory and pooled backend runtime.
"""

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from openai import OpenAI
from utils.paths import default_env_file
from agent_skills.llm_retry import (
    absolute_call_budget,
    LogicalCallDeadlineExceeded,
    NonRetryableGatewayError,
    RetryableGatewayError,
    call_with_gateway_retry,
    is_retryable_gateway_error,
    is_terminal_gateway_error,
    logical_call_budget,
    logical_deadline,
    remaining_timeout,
    safe_gateway_error_code,
    safe_gateway_error_metadata,
)
from agent_skills.responses_stream import (
    ResponsesProtocolError,
    ResponsesStreamError,
    configured_responses_streaming,
    invoke_responses,
)
from agent_skills.responses_diagnostics import (
    attach_responses_diagnostics,
    format_responses_failure,
    get_responses_diagnostics,
)
from agent_skills.llm_timing import measure_llm_request

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - depends on optional local deps
    def load_dotenv(path=None, *args, **kwargs):
        env_path = Path(path or default_env_file())
        if not env_path.exists():
            return False
        values: Dict[str, str] = {}
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            value = re.sub(
                r"\$\{([^}]+)\}",
                lambda match: values.get(match.group(1), os.getenv(match.group(1), "")),
                value,
            )
            values[key] = value
            os.environ.setdefault(key, value)
        return True

logger = logging.getLogger(__name__)


def _sanitized_gateway_exception(exc: Exception) -> Exception:
    """Drop provider text/URLs while preserving retry and terminal semantics."""

    error_type, http_status = safe_gateway_error_metadata(exc)
    terminal = is_terminal_gateway_error(exc)
    error_class = (
        RetryableGatewayError
        if is_retryable_gateway_error(exc)
        else NonRetryableGatewayError
    )
    status_suffix = (
        f" http_status={http_status}" if http_status is not None else ""
    )
    sanitized = error_class(
        f"Device gateway request failed: {error_type}{status_suffix}"
    )
    if http_status is not None:
        setattr(sanitized, "status_code", http_status)
    safe_code = safe_gateway_error_code(exc)
    if safe_code is not None:
        setattr(sanitized, "_chem_gateway_error_code", safe_code)
    if terminal:
        # Preserve the auth/quota decision as metadata, without retaining a
        # provider-controlled code/message. Pools use this bit to avoid retrying
        # another backend that has the same endpoint and credential.
        setattr(sanitized, "_chem_terminal_gateway_error", True)
    diagnostics = get_responses_diagnostics(exc)
    if diagnostics is not None:
        attach_responses_diagnostics(sanitized, diagnostics)
    return sanitized


DEFAULT_LLM_MAX_RETRIES = 8


def configured_max_retries() -> int:
    """Return the transport retry count used by every OpenAI-compatible client."""

    raw_value = os.getenv("REFINER_LLM_MAX_RETRIES", str(DEFAULT_LLM_MAX_RETRIES))
    try:
        return max(0, int(raw_value))
    except ValueError:
        logger.warning(
            "Invalid REFINER_LLM_MAX_RETRIES=%r; using %s",
            raw_value,
            DEFAULT_LLM_MAX_RETRIES,
        )
        return DEFAULT_LLM_MAX_RETRIES


def normalize_openai_base_url(base_url: str) -> str:
    """Ensure an OpenAI-compatible base URL includes the ``/v1`` prefix."""

    value = str(base_url or "").strip().rstrip("/")
    if not value:
        return value
    parsed = urlsplit(value)
    path = parsed.path.rstrip("/")
    if path == "/v1" or path.endswith("/v1"):
        return value
    path = f"{path}/v1" if path else "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)).rstrip("/")


def _is_official_kimi_k3_chat(base_url: str, model: str) -> bool:
    """Identify K3 on an official Kimi Chat Completions API root."""

    parsed = urlsplit(normalize_openai_base_url(base_url))
    if parsed.scheme != "https":
        return False
    return (parsed.hostname, parsed.path.rstrip("/"), model.lower()) in {
        ("api.moonshot.ai", "/v1", "kimi-k3"),
        ("api.kimi.ai", "/coding/v1", "k3"),
        ("api.kimi.ai", "/coding/v1", "k3-256k"),
        ("api.kimi.com", "/coding/v1", "k3"),
        ("api.kimi.com", "/coding/v1", "k3-256k"),
    }


_DEVICE_JSON_ENVELOPE_FIELD = "payload_json"
_DEVICE_JSON_ENVELOPE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        _DEVICE_JSON_ENVELOPE_FIELD: {
            "type": "string",
            "description": (
                "The exact requested Device response serialized as one JSON "
                "object string, with no Markdown or surrounding prose."
            ),
        }
    },
    "required": [_DEVICE_JSON_ENVELOPE_FIELD],
    "additionalProperties": False,
}


class _CliOutputSchemaRejected(RuntimeError):
    """Internal signal for a sanitized CLI output-schema rejection."""


@dataclass
class ChatResponse:
    """Lightweight response wrapper compatible with `.content` access."""

    content: str
    reasoning_content: str = ""
    raw_response: Any = None


@dataclass
class BackendConfig:
    """Single backend configuration inside a model pool."""

    name: str
    provider: str
    model_name: str
    api_key: str
    endpoint_url: str


class CodexResponsesModel:
    """Stateless Responses API adapter with an optional Codex CLI fallback.

    The normal path sends one plain Responses request with no tools.  Running a
    full ``codex exec`` agent for every JSON subtask is both slower and less
    deterministic because the CLI may invoke workspace tools before answering.
    """

    handles_request_timing = True

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        reasoning_effort: str = "xhigh",
        timeout: float = 180.0,
        max_output_tokens: Optional[int] = None,
        codex_path: Optional[str] = None,
        client: Optional[Any] = None,
    ) -> None:
        self.model_name = model
        self.api_key = api_key
        self.base_url = normalize_openai_base_url(base_url)
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self._transport_max_retries = configured_max_retries()
        self.handles_transport_retries = True
        self.codex_path = (
            codex_path
            or shutil.which("codex")
            or "/Applications/Codex.app/Contents/Resources/codex"
        )
        self._client = client or OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            # The shared middleware is the sole transport-retry owner.
            max_retries=0,
        )
        # None means unprobed.  A deterministic CLI/gateway rejection or a
        # successful response that ignores the envelope disables the feature
        # for later JSON calls on this model instance.
        self._cli_output_schema_supported: Optional[bool] = None

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Delegate native Responses conversion without invoking Codex CLI."""
        from agent_skills.native_tools import make_native_openai_model, require_direct_tool_transport

        require_direct_tool_transport()
        if getattr(self, "_native_tool_model", None) is None:
            self._native_tool_model = make_native_openai_model(
                model=self.model_name,
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_tokens=self.max_output_tokens,
                max_retries=self._transport_max_retries,
                use_responses_api=True,
                reasoning_effort=self.reasoning_effort,
            )
        return self._native_tool_model.bind_tools(tools, **kwargs)

    def invoke(self, messages: List[Any]) -> ChatResponse:
        deadline = logical_deadline(self.timeout)
        prompt = self._messages_to_prompt(messages)
        transport = os.getenv("REFINER_RESPONSES_TRANSPORT", "direct").strip().lower()
        if transport not in {"cli", "codex_cli"}:
            try:
                return call_with_gateway_retry(
                    lambda: self._invoke_direct(prompt),
                    max_retries=self._transport_max_retries,
                    deadline=deadline,
                    logger=logger,
                    operation_name="Device Responses request",
                )
            except Exception as exc:
                if isinstance(
                    exc, (LogicalCallDeadlineExceeded, ResponsesStreamError)
                ):
                    # A broken or incomplete stream must not replay this task
                    # through the historical CLI fallback.
                    raise
                if (
                    configured_responses_streaming()
                    or is_retryable_gateway_error(exc)
                    or is_terminal_gateway_error(exc)
                ):
                    # SDK exceptions may carry provider response bodies and
                    # request URLs. Preserve only bounded classification
                    # metadata at the public Device boundary.
                    raise _sanitized_gateway_exception(exc) from None
                fallback = os.getenv(
                    "REFINER_RESPONSES_CLI_FALLBACK", "1"
                ).strip().lower()
                if fallback in {"0", "off", "false", "no"}:
                    raise
                safe_failure = format_responses_failure(exc)
                if safe_failure is not None:
                    logger.warning(
                        "Direct Responses request failed; falling back to Codex CLI: %s",
                        safe_failure,
                    )
                else:
                    error_type, http_status = safe_gateway_error_metadata(exc)
                    logger.warning(
                        "Direct Responses request failed; falling back to Codex CLI: %s%s",
                        error_type,
                        (
                            f" http_status={http_status}"
                            if http_status is not None
                            else ""
                        ),
                    )
        return call_with_gateway_retry(
            lambda: self._invoke_cli(prompt),
            max_retries=self._transport_max_retries,
            deadline=deadline,
            logger=logger,
            operation_name="Device Codex CLI request",
        )

    def invoke_json_object(self, messages: List[Any]) -> ChatResponse:
        """Invoke a Device JSON task with a CLI structured-output guard.

        The public ``invoke`` method deliberately keeps its historical text
        behavior.  Device call sites that require exactly one JSON object can
        opt into this method.  Direct Responses requests are also left
        unchanged because some OpenAI-compatible gateways reject SDK-shaped
        structured-output parameters.  On the Codex CLI transport, a tiny
        strict envelope prevents prose or multiple top-level values while the
        existing Device parser remains responsible for validating the inner
        task-specific object.
        """
        deadline = logical_deadline(self.timeout)
        prompt = self._messages_to_prompt(messages)
        transport = os.getenv("REFINER_RESPONSES_TRANSPORT", "direct").strip().lower()
        if transport not in {"cli", "codex_cli"}:
            try:
                return call_with_gateway_retry(
                    lambda: self._invoke_direct(prompt),
                    max_retries=self._transport_max_retries,
                    deadline=deadline,
                    logger=logger,
                    operation_name="Device Responses JSON request",
                )
            except Exception as exc:
                if isinstance(
                    exc, (LogicalCallDeadlineExceeded, ResponsesStreamError)
                ):
                    raise
                if (
                    configured_responses_streaming()
                    or is_retryable_gateway_error(exc)
                    or is_terminal_gateway_error(exc)
                ):
                    raise _sanitized_gateway_exception(exc) from None
                fallback = os.getenv(
                    "REFINER_RESPONSES_CLI_FALLBACK", "1"
                ).strip().lower()
                if fallback in {"0", "off", "false", "no"}:
                    raise
                logger.warning(
                    "Direct Responses JSON request failed with %s; falling "
                    "back to the Codex CLI",
                    type(exc).__name__,
                )
        return call_with_gateway_retry(
            lambda: self._invoke_cli_json_object(prompt),
            max_retries=self._transport_max_retries,
            deadline=deadline,
            logger=logger,
            operation_name="Device Codex CLI JSON request",
        )

    def _invoke_direct(self, prompt: str) -> ChatResponse:
        payload: Dict[str, Any] = {
            "model": self.model_name,
            "input": prompt,
            "store": False,
        }
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        if self.max_output_tokens is not None:
            payload["max_output_tokens"] = self.max_output_tokens
        with measure_llm_request(
            component=os.getenv("CHEM_LLM_COMPONENT", "device"),
            model=self.model_name,
            transport="device_responses",
        ) as timing:
            response = invoke_responses(
                self._client, payload, on_event=timing.observe,
            )
        text = self._extract_response_text(response)
        if not text:
            status = getattr(response, "status", "")
            raise RuntimeError(
                f"Responses API returned no text output (status={status or 'unknown'})"
            )
        return ChatResponse(content=text, raw_response=response)

    def _invoke_cli(self, prompt: str) -> ChatResponse:
        return self._invoke_cli_request(prompt, request_json_envelope=False)

    def _invoke_cli_json_object(self, prompt: str) -> ChatResponse:
        return self._invoke_cli_request(prompt, request_json_envelope=True)

    def _invoke_cli_request(
        self,
        prompt: str,
        *,
        request_json_envelope: bool,
    ) -> ChatResponse:
        tmpdir = tempfile.mkdtemp(prefix="device-codex-")
        try:
            tmp_path = Path(tmpdir)
            output_path = tmp_path / "last_message.txt"
            self._write_codex_home(tmp_path)
            schema_path: Optional[Path] = None
            use_json_envelope = (
                request_json_envelope
                and self._cli_output_schema_supported is not False
            )
            if use_json_envelope:
                schema_path = tmp_path / "device_json_envelope.schema.json"
                schema_path.write_text(
                    json.dumps(
                        _DEVICE_JSON_ENVELOPE_SCHEMA,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
                schema_path.chmod(0o600)
            stateless_prompt = (
                "You are serving one stateless structured-inference request. "
                "All evidence needed to answer is already included below. Do not call "
                "tools, inspect files, browse the network, or discuss your work. "
                + (
                    "Serialize the exact requested final JSON object into the "
                    f"{_DEVICE_JSON_ENVELOPE_FIELD} string required by the output "
                    "schema; put no prose or Markdown outside it.\n\n"
                    if use_json_envelope
                    else (
                        "Return exactly one requested final JSON object with no "
                        "prose or Markdown.\n\n"
                        if request_json_envelope
                        else "Return the requested final JSON/text directly.\n\n"
                    )
                )
                + prompt
            )
            env = dict(os.environ)
            env["CODEX_HOME"] = str(tmp_path)
            # Codex authenticates from the private auth.json in CODEX_HOME.
            # Keeping API keys out of the child environment prevents them from
            # being copied into shell snapshots if a CLI process is interrupted.
            env.pop("OPENAI_API_KEY", None)
            env.pop("REFINER_LLM_API_KEY", None)

            try:
                completed = self._run_checked_codex_cli(
                    stateless_prompt=stateless_prompt,
                    output_path=output_path,
                    schema_path=schema_path if use_json_envelope else None,
                    env=env,
                    cwd=tmp_path,
                    allow_schema_rejection_fallback=use_json_envelope,
                )
            except _CliOutputSchemaRejected:
                self._cli_output_schema_supported = False
                logger.warning(
                    "Codex CLI output-schema is unsupported by this CLI/gateway; "
                    "retrying this JSON request once without the schema"
                )
                return self._run_plain_cli_fallback(
                    prompt=prompt,
                    output_path=output_path,
                    env=env,
                    cwd=tmp_path,
                )

            try:
                text = self._read_cli_output(output_path)
            except RuntimeError:
                if not use_json_envelope:
                    raise
                self._cli_output_schema_supported = False
                logger.warning(
                    "Codex CLI schema attempt produced no readable final output; "
                    "retrying this JSON request once without the schema"
                )
                return self._run_plain_cli_fallback(
                    prompt=prompt,
                    output_path=output_path,
                    env=env,
                    cwd=tmp_path,
                )
            if not use_json_envelope:
                return ChatResponse(content=text)

            inner = self._unwrap_json_envelope(text)
            if inner is not None:
                self._cli_output_schema_supported = True
                return ChatResponse(content=inner)

            # A zero-exit response that does not match the requested envelope
            # means the CLI/gateway ignored or failed to enforce the schema.
            # Never guess at or partially extract the payload.  Retry once via
            # the old plain transport, then cache the incompatibility.
            self._cli_output_schema_supported = False
            logger.warning(
                "Codex CLI returned a non-conforming JSON envelope; retrying "
                "this JSON request once without the schema (%s)",
                self._text_diagnostic(text),
            )
            return self._run_plain_cli_fallback(
                prompt=prompt,
                output_path=output_path,
                env=env,
                cwd=tmp_path,
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _run_plain_cli_fallback(
        self,
        *,
        prompt: str,
        output_path: Path,
        env: Dict[str, str],
        cwd: Path,
    ) -> ChatResponse:
        """Run exactly one schema-free CLI fallback inside the same temp home."""
        stateless_prompt = (
            "You are serving one stateless structured-inference request. "
            "All evidence needed to answer is already included below. Do not call "
            "tools, inspect files, browse the network, or discuss your work. Return "
            "exactly one requested final JSON object with no prose or Markdown.\n\n"
            + prompt
        )
        self._run_checked_codex_cli(
            stateless_prompt=stateless_prompt,
            output_path=output_path,
            schema_path=None,
            env=env,
            cwd=cwd,
            allow_schema_rejection_fallback=False,
        )
        return ChatResponse(content=self._read_cli_output(output_path))

    def _run_checked_codex_cli(
        self,
        *,
        stateless_prompt: str,
        output_path: Path,
        schema_path: Optional[Path],
        env: Dict[str, str],
        cwd: Path,
        allow_schema_rejection_fallback: bool,
    ) -> subprocess.CompletedProcess[str]:
        """Run and validate one measured, provider-text-safe CLI attempt."""

        with measure_llm_request(
            component=os.getenv("CHEM_LLM_COMPONENT", "device"),
            model=self.model_name,
            transport="device_codex_cli",
        ):
            completed = self._run_codex_cli(
                stateless_prompt=stateless_prompt,
                output_path=output_path,
                schema_path=schema_path,
                env=env,
                cwd=cwd,
            )
            if completed.returncode == 0:
                return completed
            diagnostic = self._cli_failure_diagnostic(completed)
            if (
                allow_schema_rejection_fallback
                and self._is_output_schema_rejection(completed)
            ):
                raise _CliOutputSchemaRejected(diagnostic)
            if self._is_retryable_cli_failure(completed):
                raise RetryableGatewayError(diagnostic)
            raise RuntimeError(diagnostic)

    def _run_codex_cli(
        self,
        *,
        stateless_prompt: str,
        output_path: Path,
        schema_path: Optional[Path],
        env: Dict[str, str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        output_path.unlink(missing_ok=True)
        cmd = [
            self.codex_path,
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
        ]
        if schema_path is not None:
            cmd.extend(["--output-schema", str(schema_path)])
        cmd.extend(
            [
                "--output-last-message",
                str(output_path),
                "--json",
                "-",
            ]
        )
        try:
            return subprocess.run(
                cmd,
                input=stateless_prompt,
                text=True,
                capture_output=True,
                timeout=self.timeout,
                env=env,
                cwd=cwd,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Codex responses call timed out after {self.timeout:g} seconds"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"Codex responses CLI launch failed ({type(exc).__name__})"
            ) from exc

    @staticmethod
    def _read_cli_output(output_path: Path) -> str:
        if not output_path.exists():
            raise RuntimeError("Codex responses call did not produce output-last-message")
        text = output_path.read_text(encoding="utf-8").strip()
        if not text:
            raise RuntimeError("Codex responses call returned empty output")
        return text

    @staticmethod
    def _unwrap_json_envelope(text: str) -> Optional[str]:
        try:
            envelope = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(envelope, dict):
            return None
        if set(envelope) != {_DEVICE_JSON_ENVELOPE_FIELD}:
            return None
        payload = envelope.get(_DEVICE_JSON_ENVELOPE_FIELD)
        return payload if isinstance(payload, str) else None

    @staticmethod
    def _is_output_schema_rejection(completed: subprocess.CompletedProcess[str]) -> bool:
        detail = f"{completed.stderr or ''}\n{completed.stdout or ''}".lower()
        schema_markers = (
            "--output-schema",
            "output_schema",
            "output schema",
            "json_schema",
            "response_format",
            "text.format",
        )
        rejection_markers = (
            "unsupported",
            "not supported",
            "unknown",
            "unrecognized",
            "unexpected argument",
            "invalid parameter",
            "invalid schema",
            "invalid value",
        )
        return any(marker in detail for marker in schema_markers) and any(
            marker in detail for marker in rejection_markers
        )

    @classmethod
    def _is_retryable_cli_failure(
        cls,
        completed: subprocess.CompletedProcess[str],
    ) -> bool:
        """Classify raw CLI output without exposing it beyond this method."""

        detail = f"{completed.stdout or ''}\n{completed.stderr or ''}".lower()
        status_code = cls._cli_gateway_status_code(detail)
        if status_code is not None:
            return status_code in {408, 409, 425, 429} or 500 <= status_code <= 599
        return any(
            marker in detail
            for marker in (
                "bad gateway",
                "gateway timeout",
                "origin_bad_gateway",
                "too many requests",
                "too early",
                "request timeout",
                "rate limit",
                "service unavailable",
                "internal server error",
                "temporarily unavailable",
                "upstream connect error",
                "upstream request timeout",
                "stream disconnected",
                "connection closed",
                "connection reset",
                "connection refused",
                "connection timed out",
                "read timed out",
                "write timed out",
                "pool timeout",
                "transport error",
                "network error",
                "error sending request",
                "deadline exceeded",
                "unexpected eof",
            )
        )

    @staticmethod
    def _cli_gateway_status_code(detail: str) -> Optional[int]:
        status_patterns = (
            re.compile(
                r"\b(?:http(?:/\d(?:\.\d)?)?|unexpected\s+status|last\s+status|"
                r"status(?:[\s_-]+code)?|error(?:\s+code)?|code|"
                r"(?:gateway|upstream)(?:\s+(?:returned|response|status))?)"
                r"[\"']?\s*[:=]?\s*[\"']?([1-5]\d{2})\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\b([1-5]\d{2})\s+(?:bad\s+gateway|gateway\s+timeout|"
                r"too\s+many\s+requests|request\s+timeout|service\s+unavailable)",
                re.IGNORECASE,
            ),
        )
        matches = [
            match
            for pattern in status_patterns
            for match in pattern.finditer(detail)
        ]
        if not matches:
            return None
        last_match = max(matches, key=lambda match: match.start())
        try:
            return int(last_match.group(1))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _cli_failure_diagnostic(
        cls,
        completed: subprocess.CompletedProcess[str],
    ) -> str:
        return (
            "Codex responses call failed: "
            f"exit_code={completed.returncode}, "
            f"stdout_{cls._text_diagnostic(completed.stdout or '')}, "
            f"stderr_{cls._text_diagnostic(completed.stderr or '')}"
        )

    @staticmethod
    def _text_diagnostic(text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        return f"len={len(text)},sha256={digest}"

    @staticmethod
    def _extract_response_text(response: Any) -> str:
        direct = getattr(response, "output_text", None)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        parts: List[str] = []
        output = getattr(response, "output", None)
        if output is None and isinstance(response, dict):
            output = response.get("output")
        for item in output if isinstance(output, list) else []:
            content = getattr(item, "content", None)
            if content is None and isinstance(item, dict):
                content = item.get("content")
            for block in content if isinstance(content, list) else []:
                text = getattr(block, "text", None)
                if text is None and isinstance(block, dict):
                    text = block.get("text") or block.get("content")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()

    def _write_codex_home(self, path: Path) -> None:
        bundled_marketplace = Path(
            os.getenv(
                "REFINER_CODEX_BUNDLED_MARKETPLACE",
                str(Path.home() / ".codex/.tmp/bundled-marketplaces/openai-bundled"),
            )
        )
        primary_runtime_marketplace = Path(
            os.getenv(
                "REFINER_CODEX_PRIMARY_RUNTIME_MARKETPLACE",
                str(
                    Path.home()
                    / ".cache/codex-runtimes/codex-primary-runtime/plugins/openai-primary-runtime"
                ),
            )
        )
        config = f"""model_provider = "OpenAI"
model = "{self._escape_toml(self.model_name)}"
model_reasoning_effort = "{self._escape_toml(self.reasoning_effort)}"
disable_response_storage = true
network_access = "enabled"
model_context_window = 1000000
model_auto_compact_token_limit = 900000

[model_providers.OpenAI]
name = "OpenAI"
base_url = "{self._escape_toml(self.base_url)}"
wire_api = "responses"
requires_openai_auth = true
request_max_retries = 0
stream_max_retries = 0
"""
        load_marketplaces = os.getenv(
            "REFINER_CODEX_LOAD_MARKETPLACES", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        if load_marketplaces and bundled_marketplace.exists():
            config += f"""
[marketplaces.openai-bundled]
source_type = "local"
source = "{self._escape_toml(str(bundled_marketplace))}"
"""
        if load_marketplaces and primary_runtime_marketplace.exists():
            config += f"""
[marketplaces.openai-primary-runtime]
source_type = "local"
source = "{self._escape_toml(str(primary_runtime_marketplace))}"
"""
        config_path = path / "config.toml"
        auth_path = path / "auth.json"
        config_path.write_text(config, encoding="utf-8")
        auth_path.write_text(
            json.dumps({"OPENAI_API_KEY": self.api_key}, ensure_ascii=False),
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        auth_path.chmod(0o600)

    def _messages_to_prompt(self, messages: List[Any]) -> str:
        parts = []
        for message in messages:
            content = getattr(message, "content", None)
            if content is None and isinstance(message, dict):
                content = message.get("content")
            role = getattr(message, "type", None) or getattr(message, "role", None)
            if role is None and isinstance(message, dict):
                role = message.get("role")
            label = str(role or "message")
            if content:
                parts.append(f"[{label}]\n{content}")
        return "\n\n".join(parts)

    @staticmethod
    def _escape_toml(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')


class OpenAICompatChatModel:
    """Minimal OpenAI-compatible chat model wrapper for one backend."""

    handles_request_timing = True

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout: int,
        temperature: float,
        max_tokens: Optional[int] = None,
        disable_thinking: bool = True,
        do_sample: bool = False,
        name: Optional[str] = None,
    ) -> None:
        self.model_name = model
        self._preferred_model = model
        self.api_key = api_key
        self.base_url = normalize_openai_base_url(base_url)
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.disable_thinking = disable_thinking
        self.do_sample = do_sample
        self.name = name or f"{self.base_url}:{model}"
        self._transport_max_retries = configured_max_retries()
        self.handles_transport_retries = True
        self._client = OpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0,
        )
        self._supports_extra_body: Optional[bool] = None

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Keep native messages/call IDs separate from the legacy text wrapper."""
        from agent_skills.native_tools import make_native_openai_model, require_direct_tool_transport

        responses = os.getenv("REFINER_LLM_WIRE_API", "").strip().lower() == "codex_responses"
        if responses:
            require_direct_tool_transport()
        cache_key = (responses, self._preferred_model)
        if getattr(self, "_native_tool_model_key", None) != cache_key:
            self._native_tool_model = make_native_openai_model(
                model=self._preferred_model or self.model_name,
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_tokens=self.max_tokens,
                max_retries=self._transport_max_retries,
                use_responses_api=responses,
                reasoning_effort=os.getenv("REFINER_LLM_REASONING_EFFORT", "xhigh"),
                temperature=(
                    None
                    if _is_official_kimi_k3_chat(self.base_url, self._preferred_model)
                    else self.temperature
                ),
            )
            self._native_tool_model_key = cache_key
        return self._native_tool_model.bind_tools(tools, **kwargs)

    def _coerce_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    text = item.strip()
                    if text:
                        parts.append(text)
                    continue
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        text = text.strip()
                        if text:
                            parts.append(text)
                        continue
                    if item.get("type") == "text" and item.get("content"):
                        text = str(item["content"]).strip()
                        if text:
                            parts.append(text)
                        continue
                if item is not None:
                    text = str(item).strip()
                    if text:
                        parts.append(text)
            return "\n".join(parts).strip()
        if content is None:
            return ""
        return str(content).strip()

    def _normalize_messages(self, messages: List[Any]) -> List[Dict[str, str]]:
        normalized: List[Dict[str, str]] = []
        for message in messages:
            if isinstance(message, dict):
                role = str(message.get("role") or "user")
                content = self._coerce_content(message.get("content"))
            else:
                role = getattr(message, "type", None) or getattr(message, "role", None) or "user"
                content = self._coerce_content(getattr(message, "content", ""))

            role = {
                "human": "user",
                "ai": "assistant",
                "system": "system",
            }.get(role, role)
            normalized.append({"role": role, "content": content})
        return normalized

    def _candidate_models(self) -> List[str]:
        return [self._preferred_model or self.model_name]

    def _build_payload(self, messages: List[Any], include_extra_body: bool) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "messages": self._normalize_messages(messages),
            "timeout": remaining_timeout(self.timeout),
        }
        is_kimi_k3 = _is_official_kimi_k3_chat(self.base_url, self._preferred_model)
        if not is_kimi_k3:
            payload["temperature"] = self.temperature
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens

        if include_extra_body and not is_kimi_k3:
            extra_body: Dict[str, Any] = {}
            if self.disable_thinking:
                extra_body["thinking"] = {"type": "disabled"}
            if not self.do_sample:
                extra_body["do_sample"] = False
            if extra_body:
                payload["extra_body"] = extra_body
        return payload

    def _is_deterministic_extra_body_rejection(self, exc: Exception) -> bool:
        message = str(exc).lower()
        markers = [
            "format mismatch",
            "only [['openai_chat', 'openai_responses']] is allowed",
            "only [[\"openai_chat\", \"openai_responses\"]] is allowed",
            "unsupported extra_body",
            "unknown parameter: extra_body",
        ]
        return any(marker in message for marker in markers)

    def _payload_variants(self, messages: List[Any]) -> List[Tuple[str, Dict[str, Any]]]:
        variants: List[Tuple[str, Dict[str, Any]]] = []
        compat_payload = self._build_payload(messages, include_extra_body=True)
        plain_payload = self._build_payload(messages, include_extra_body=False)

        if "extra_body" in compat_payload and self._supports_extra_body is not False:
            variants.append(("compat", compat_payload))
            variants.append(("plain", plain_payload))
        else:
            variants.append(("plain", plain_payload))
        return variants

    def invoke(self, messages: List[Any]) -> ChatResponse:
        deadline = logical_deadline(self.timeout)
        last_error: Optional[Exception] = None

        for model_name in self._candidate_models():
            for payload_label, payload in self._payload_variants(messages):
                try:
                    def request() -> Any:
                        with measure_llm_request(
                            component=os.getenv("CHEM_LLM_COMPONENT", "device"),
                            model=model_name,
                            transport="device_chat",
                        ):
                            return self._client.chat.completions.create(
                                model=model_name, **payload
                            )

                    response = call_with_gateway_retry(
                        request,
                        max_retries=self._transport_max_retries,
                        deadline=deadline,
                        logger=logger,
                        operation_name="Device chat request",
                    )
                    message = response.choices[0].message if response and response.choices else None
                    content = self._coerce_content(getattr(message, "content", None))
                    reasoning_content = self._coerce_content(getattr(message, "reasoning_content", None))
                    if not content:
                        raise ValueError(f"LLM returned empty content for model {model_name}")

                    if payload_label == "compat":
                        self._supports_extra_body = True
                    if payload_label == "plain" and self._supports_extra_body is not False and (
                        self.disable_thinking or not self.do_sample
                    ):
                        logger.warning(
                            "LLM backend recovered by retrying model %s without extra_body",
                            model_name,
                        )

                    self._preferred_model = model_name
                    return ChatResponse(
                        content=content,
                        reasoning_content=reasoning_content,
                        raw_response=response,
                    )
                except Exception as exc:
                    last_error = exc
                    if payload_label == "compat" and self._is_deterministic_extra_body_rejection(exc):
                        if self._supports_extra_body is not False:
                            logger.warning(
                                "LLM backend detected deterministic extra_body rejection on model %s; "
                                "future calls will skip compat payloads for this backend",
                                model_name,
                            )
                        self._supports_extra_body = False
                    error_type, http_status = safe_gateway_error_metadata(exc)
                    logger.warning(
                        "LLM backend model %s failed with %s payload: %s%s",
                        model_name,
                        payload_label,
                        error_type,
                        (
                            f" http_status={http_status}"
                            if http_status is not None
                            else ""
                        ),
                    )
                    if isinstance(
                        exc, (LogicalCallDeadlineExceeded, ResponsesProtocolError)
                    ):
                        raise
                    if is_retryable_gateway_error(exc) or is_terminal_gateway_error(exc):
                        raise _sanitized_gateway_exception(exc) from None
                    continue

        if last_error is not None:
            _, http_status = safe_gateway_error_metadata(last_error)
            if http_status is not None:
                raise _sanitized_gateway_exception(last_error) from None
            raise last_error
        raise RuntimeError("No available model candidates for configured backend")


class ModelPoolChatModel:
    """Pooled model wrapper that fails over across multiple backends."""

    def __init__(
        self,
        backends: List[OpenAICompatChatModel],
        *,
        max_rounds: int = 2,
        round_backoff_seconds: Optional[List[int]] = None,
        wall_timeout_seconds: Optional[float] = None,
    ) -> None:
        if not backends:
            raise ValueError("ModelPoolChatModel requires at least one backend")
        self.backends = backends
        self.max_rounds = max(1, max_rounds)
        self.round_backoff_seconds = round_backoff_seconds or [2, 5]
        self._preferred_backend_index = 0
        # Concrete backends already own their bounded transport retry.  The
        # pool may fail over once across distinct backends, but must not replay
        # the whole pool in additional rounds.
        self.handles_transport_retries = True
        self.handles_request_timing = True
        self._transport_max_retries = 0
        if wall_timeout_seconds is None:
            candidate = getattr(backends[0], "timeout", 240.0)
            try:
                wall_timeout_seconds = float(candidate)
            except (TypeError, ValueError):
                # Lightweight fake/custom backends do not necessarily expose
                # a numeric timeout. Production construction passes this
                # value explicitly from ``LLMFactory.create``.
                wall_timeout_seconds = 240.0
        self.wall_timeout_seconds = float(wall_timeout_seconds)
        if not math.isfinite(self.wall_timeout_seconds) or self.wall_timeout_seconds <= 0:
            raise ValueError("ModelPoolChatModel wall timeout must be finite and positive")

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Failover only between native-bound backends, never text invoke()."""
        return NativeBoundModelPool(self, tools, kwargs)

    def _ordered_backend_entries(self) -> List[Tuple[int, OpenAICompatChatModel]]:
        if len(self.backends) == 1:
            return [(0, self.backends[0])]

        ordered: List[Tuple[int, OpenAICompatChatModel]] = []
        start = self._preferred_backend_index % len(self.backends)
        for offset in range(len(self.backends)):
            index = (start + offset) % len(self.backends)
            ordered.append((index, self.backends[index]))
        return ordered

    @staticmethod
    def _backend_slice_deadline(
        deadline: float,
        candidates_left: int,
    ) -> float:
        """Reserve a fair share of the remaining wall budget for failover."""

        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            raise LogicalCallDeadlineExceeded(
                "LLM model pool exhausted its shared wall budget"
            )
        return now + remaining / max(1, candidates_left)

    @staticmethod
    def _backend_auth_identity(backend: Any) -> str:
        """Return a non-secret identity for one endpoint/credential pair."""

        base_url = getattr(backend, "base_url", None)
        api_key = getattr(backend, "api_key", None)
        if isinstance(base_url, str) and isinstance(api_key, str):
            material = f"{base_url.rstrip('/')}\0{api_key}".encode("utf-8")
            return hashlib.sha256(material).hexdigest()
        # Unknown/custom backends cannot be proven to share credentials, so
        # preserve the pool's normal failover contract between distinct
        # backend objects.
        return f"backend-object:{id(backend)}"

    def invoke(self, messages: List[Any]) -> ChatResponse:
        # One user-visible model invocation gets one wall-clock budget.  Each
        # concrete backend derives its own transport deadline through
        # ``logical_deadline()``, whose ambient clamp prevents failover from
        # resetting the clock for every backend.
        with logical_call_budget(self.wall_timeout_seconds):
            deadline = logical_deadline(self.wall_timeout_seconds)
            return self._invoke_with_shared_deadline(messages, deadline)

    def _invoke_with_shared_deadline(
        self,
        messages: List[Any],
        deadline: float,
    ) -> ChatResponse:
        last_error: Optional[Exception] = None
        failure_notes: List[str] = []
        terminal_auth_identities: set[str] = set()

        effective_rounds = 1 if self.handles_transport_retries else self.max_rounds
        for round_index in range(effective_rounds):
            ordered_entries = self._ordered_backend_entries()
            for position, (backend_index, backend) in enumerate(ordered_entries):
                auth_identity = self._backend_auth_identity(backend)
                if auth_identity in terminal_auth_identities:
                    continue
                if time.monotonic() >= deadline:
                    raise LogicalCallDeadlineExceeded(
                        "LLM model pool exhausted its shared wall budget before failover"
                    ) from last_error
                try:
                    candidates_left = sum(
                        1
                        for _, candidate in ordered_entries[position:]
                        if self._backend_auth_identity(candidate)
                        not in terminal_auth_identities
                    )
                    child_deadline = self._backend_slice_deadline(
                        deadline, candidates_left
                    )
                    with absolute_call_budget(child_deadline):
                        response = backend.invoke(messages)
                    if time.monotonic() >= deadline:
                        raise LogicalCallDeadlineExceeded(
                            "LLM model pool backend completed after the shared wall deadline"
                        )
                    if backend_index != self._preferred_backend_index:
                        logger.warning(
                            "LLM backend failover activated: backend_%s -> backend_%s",
                            self._preferred_backend_index + 1,
                            backend_index + 1,
                        )
                    self._preferred_backend_index = backend_index
                    return response
                except Exception as exc:
                    last_error = exc
                    exc_type, http_status = safe_gateway_error_metadata(exc)
                    if isinstance(exc, ResponsesProtocolError):
                        raise
                    if (
                        isinstance(exc, LogicalCallDeadlineExceeded)
                        and time.monotonic() >= deadline
                    ):
                        raise
                    if is_terminal_gateway_error(exc):
                        # Authentication/quota failures must never retry the
                        # same endpoint/credential. A heterogeneous pool may,
                        # however, fail over once to a genuinely distinct
                        # provider identity.
                        terminal_auth_identities.add(auth_identity)
                    failure_notes.append(
                        "round {round_num}/{round_total} backend_{backend_index}: "
                        "{exc_type}{status}".format(
                            round_num=round_index + 1,
                            round_total=effective_rounds,
                            backend_index=backend_index + 1,
                            exc_type=exc_type,
                            status=(
                                f" http_status={http_status}"
                                if http_status is not None
                                else ""
                            ),
                        )
                    )
                    logger.warning(
                        "LLM backend_%s failed in pool round %s/%s: %s%s",
                        backend_index + 1,
                        round_index + 1,
                        effective_rounds,
                        exc_type,
                        (
                            f" http_status={http_status}"
                            if http_status is not None
                            else ""
                        ),
                    )

            if round_index < effective_rounds - 1:
                delay = self.round_backoff_seconds[min(round_index, len(self.round_backoff_seconds) - 1)]
                logger.warning(
                    "All LLM backends failed in pool round %s/%s, sleeping %ss before retrying the pool",
                    round_index + 1,
                    effective_rounds,
                    delay,
                )
                time.sleep(delay)

        recent_failures = " | ".join(failure_notes[-6:])
        message = (
            f"All configured LLM backends failed after {effective_rounds} pool round(s). "
            f"Recent failures: {recent_failures}"
        )
        if last_error is not None:
            # Do not let an SDK exception body, request URL, or credential
            # escape through the pool boundary. Retry/terminal classification
            # remains attached as finite metadata on the sanitized exception.
            safe_error = _sanitized_gateway_exception(last_error)
            raise RuntimeError(message) from safe_error
        raise RuntimeError(message)


class NativeBoundModelPool:
    """Transport retries preserve messages and never execute application tools."""

    def __init__(self, pool: ModelPoolChatModel, tools: Any, bind_kwargs: Dict[str, Any]) -> None:
        from agent_skills.native_tools import NativeToolConfigurationError, is_tool_support_error

        self.pool = pool
        self.handles_transport_retries = True
        self.handles_request_timing = True
        self.bound: Dict[int, Any] = {}
        self.child_handles_request_timing: Dict[int, bool] = {}
        self.child_retry_budgets: Dict[int, int] = {}
        self.unsupported: set[int] = set()
        for index, backend in enumerate(pool.backends):
            try:
                if not callable(getattr(backend, "bind_tools", None)):
                    raise NativeToolConfigurationError("Backend is text-only")
                self.bound[index] = backend.bind_tools(tools, **bind_kwargs)
                native_model = getattr(backend, "_native_tool_model", None)
                retry_value = getattr(native_model, "_chem_gateway_max_retries", None)
                # Mock/custom backends may synthesize arbitrary attributes.
                # Only a concrete integer from the native model owns a retry
                # budget; otherwise inherit the declared backend budget.
                if not isinstance(retry_value, int) or isinstance(retry_value, bool):
                    retry_value = getattr(backend, "_transport_max_retries", 0)
                self.child_retry_budgets[index] = (
                    max(0, retry_value)
                    if isinstance(retry_value, int)
                    and not isinstance(retry_value, bool)
                    else 0
                )
                underlying = getattr(self.bound[index], "bound", None)
                self.child_handles_request_timing[index] = any(
                    getattr(candidate, "handles_request_timing", False) is True
                    for candidate in (
                        self.bound[index], underlying, native_model,
                    )
                    if candidate is not None
                )
            except Exception as exc:
                if not is_tool_support_error(exc):
                    raise
                self.unsupported.add(index)
        if not self.bound:
            raise NativeToolConfigurationError("No configured pool backend supports native tools")

    def invoke(self, messages: List[Any], **kwargs: Any) -> Any:
        with logical_call_budget(self.pool.wall_timeout_seconds):
            deadline = logical_deadline(self.pool.wall_timeout_seconds)
            return self._invoke_with_shared_deadline(messages, deadline, **kwargs)

    def _invoke_with_shared_deadline(
        self,
        messages: List[Any],
        deadline: float,
        **kwargs: Any,
    ) -> Any:
        from agent_skills.native_tools import NativeToolConfigurationError, is_tool_support_error

        last_error: Optional[Exception] = None
        terminal_auth_identities: set[str] = set()
        effective_rounds = (
            1 if self.pool.handles_transport_retries else self.pool.max_rounds
        )
        for round_index in range(effective_rounds):
            ordered_entries = self.pool._ordered_backend_entries()
            for position, (index, _backend) in enumerate(ordered_entries):
                if index in self.unsupported:
                    continue
                auth_identity = self.pool._backend_auth_identity(_backend)
                if auth_identity in terminal_auth_identities:
                    continue
                if time.monotonic() >= deadline:
                    raise LogicalCallDeadlineExceeded(
                        "Native LLM model pool exhausted its shared wall budget before failover"
                    ) from last_error
                try:
                    candidates_left = sum(
                        1
                        for candidate_index, candidate in ordered_entries[position:]
                        if candidate_index not in self.unsupported
                        and self.pool._backend_auth_identity(candidate)
                        not in terminal_auth_identities
                    )
                    child_deadline = self.pool._backend_slice_deadline(
                        deadline, candidates_left
                    )
                    # Native LangChain backends do not all enter the shared
                    # retry middleware themselves (notably Chat Completions).
                    # The zero-retry bounded attempt is therefore required to
                    # preempt a blocked request-establishment/read instead of
                    # merely noticing the overrun after it eventually returns.
                    def request() -> Any:
                        if self.child_handles_request_timing.get(index, False):
                            return self.bound[index].invoke(messages, **kwargs)
                        with measure_llm_request(
                            component=os.getenv("CHEM_LLM_COMPONENT", "device"),
                            model=str(
                                getattr(_backend, "model_name", "")
                                or type(_backend).__name__
                            ),
                            transport="device_native_pool",
                        ):
                            return self.bound[index].invoke(messages, **kwargs)

                    with absolute_call_budget(child_deadline):
                        if getattr(
                            self.bound[index], "handles_transport_retries", False
                        ) is True:
                            result = request()
                        else:
                            result = call_with_gateway_retry(
                                request,
                                max_retries=self.child_retry_budgets.get(index, 0),
                                deadline=child_deadline,
                                logger=logger,
                                operation_name=(
                                    f"Device native pool backend_{index + 1} request"
                                ),
                            )
                    if time.monotonic() >= deadline:
                        raise LogicalCallDeadlineExceeded(
                            "Native LLM model pool backend completed after the shared wall deadline"
                        )
                    self.pool._preferred_backend_index = index
                    return result
                except Exception as exc:
                    if (
                        isinstance(exc, LogicalCallDeadlineExceeded)
                        and time.monotonic() >= deadline
                    ):
                        raise
                    last_error = exc
                    if is_terminal_gateway_error(exc):
                        terminal_auth_identities.add(auth_identity)
                        continue
                    if isinstance(exc, ResponsesStreamError):
                        raise
                    if is_tool_support_error(exc):
                        self.unsupported.add(index)
            if len(self.unsupported) == len(self.pool.backends):
                raise NativeToolConfigurationError(
                    "All configured backends reject native tools; no text/CLI fallback was used"
                ) from last_error
            if round_index + 1 < effective_rounds:
                delays = self.pool.round_backoff_seconds
                time.sleep(delays[min(round_index, len(delays) - 1)])
        if last_error is not None:
            safe_error = _sanitized_gateway_exception(last_error)
            raise RuntimeError("All native tool backends failed") from safe_error
        raise RuntimeError("All native tool backends failed")


class LLMFactory:
    """Create single-backend or pooled OpenAI-compatible chat models."""

    DEFAULT_PROVIDER = "openai"
    DEFAULT_MODEL_NAME = "qwen3-vl-plus"
    DEFAULT_ENDPOINT_URL = "https://apis.iflow.cn/v1"
    DEFAULT_TIMEOUT_SECONDS = 360
    DEFAULT_TEMPERATURE = 0
    DEFAULT_MAX_TOKENS = 3072
    DEFAULT_POOL_BACKOFF_SECONDS = [2, 5]
    DEFAULT_POOL_MAX_ROUNDS = 8

    @staticmethod
    def load_env(env_path: Optional[str] = None) -> None:
        if env_path is None:
            env_path = str(default_env_file())
        load_dotenv(env_path)

    @staticmethod
    def _split_csv(value: Optional[str]) -> List[str]:
        if not value:
            return []
        return [item.strip() for item in value.split(",") if item.strip()]

    @staticmethod
    def _parse_int_list(value: Optional[str], default: List[int]) -> List[int]:
        if not value:
            return list(default)

        parsed: List[int] = []
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                parsed.append(int(item))
            except ValueError:
                logger.warning("Ignoring invalid integer value in list: %s", item)
        return parsed or list(default)

    @staticmethod
    def get_config() -> Dict[str, str]:
        return {
            "provider": os.getenv("REFINER_LLM_MODEL_PROVIDER", LLMFactory.DEFAULT_PROVIDER),
            "model_name": os.getenv("REFINER_LLM_MODEL_NAME", LLMFactory.DEFAULT_MODEL_NAME),
            "api_key": os.getenv("REFINER_LLM_API_KEY", ""),
            "endpoint_url": os.getenv("REFINER_LLM_ENDPOINT_URL", LLMFactory.DEFAULT_ENDPOINT_URL),
        }

    @staticmethod
    def get_pool_config() -> List[BackendConfig]:
        backends: List[BackendConfig] = []
        index = 1

        while True:
            prefix = f"REFINER_LLM_POOL_{index}_"
            name = os.getenv(prefix + "NAME", "").strip()
            provider = os.getenv(prefix + "PROVIDER", "").strip()
            model_name = os.getenv(prefix + "MODEL_NAME", "").strip()
            api_key = os.getenv(prefix + "API_KEY", "").strip()
            endpoint_url = os.getenv(prefix + "ENDPOINT_URL", "").strip()

            if not any([name, provider, model_name, api_key, endpoint_url]):
                break

            backends.append(
                BackendConfig(
                    name=name or f"pool_backend_{index}",
                    provider=provider or LLMFactory.DEFAULT_PROVIDER,
                    model_name=model_name,
                    api_key=api_key,
                    endpoint_url=endpoint_url or LLMFactory.DEFAULT_ENDPOINT_URL,
                )
            )
            index += 1

        return backends

    @staticmethod
    def get_pool_runtime_config() -> Dict[str, Any]:
        return {
            "max_rounds": int(
                os.getenv(
                    "REFINER_LLM_POOL_MAX_ROUNDS",
                    str(LLMFactory.DEFAULT_POOL_MAX_ROUNDS),
                )
            ),
            "round_backoff_seconds": LLMFactory._parse_int_list(
                os.getenv("REFINER_LLM_POOL_BACKOFF_SECONDS"),
                LLMFactory.DEFAULT_POOL_BACKOFF_SECONDS,
            ),
        }

    @staticmethod
    def _build_single_backend_model(
        *,
        provider: str,
        model_name: str,
        api_key: str,
        endpoint_url: str,
        timeout: int,
        temperature: float,
        max_tokens: int,
        disable_thinking: bool,
        do_sample: bool,
        backend_name: Optional[str] = None,
    ) -> OpenAICompatChatModel:
        if provider != "openai":
            raise ValueError(f"Unsupported provider: {provider}")

        return OpenAICompatChatModel(
            model=model_name,
            api_key=api_key,
            base_url=endpoint_url,
            timeout=timeout,
            temperature=temperature,
            max_tokens=max_tokens,
            disable_thinking=disable_thinking,
            do_sample=do_sample,
            name=backend_name,
        )

    @staticmethod
    def create(
        provider: Optional[str] = None,
        model_name: Optional[str] = None,
        api_key: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        **kwargs,
    ) -> Any:
        LLMFactory.load_env()
        config = LLMFactory.get_config()

        explicit_single_backend = any(value is not None for value in [provider, model_name, api_key, endpoint_url])

        timeout = kwargs.pop("timeout", LLMFactory.DEFAULT_TIMEOUT_SECONDS)
        temperature = kwargs.pop("temperature", LLMFactory.DEFAULT_TEMPERATURE)
        max_tokens = kwargs.pop("max_tokens", LLMFactory.DEFAULT_MAX_TOKENS)
        disable_thinking = kwargs.pop("disable_thinking", True)
        do_sample = kwargs.pop("do_sample", False)
        pool_backends = kwargs.pop("pool_backends", None)
        pool_max_rounds = kwargs.pop("pool_max_rounds", None)
        pool_backoff_seconds = kwargs.pop("pool_backoff_seconds", None)

        if kwargs:
            unexpected = ", ".join(sorted(kwargs.keys()))
            raise TypeError(f"Unexpected keyword arguments for LLMFactory.create(): {unexpected}")

        if pool_backends is None and not explicit_single_backend:
            pool_backends = LLMFactory.get_pool_config()

        if pool_backends:
            runtime_config = LLMFactory.get_pool_runtime_config()
            models: List[OpenAICompatChatModel] = []

            for index, backend in enumerate(pool_backends, 1):
                if isinstance(backend, BackendConfig):
                    backend_config = backend
                else:
                    backend_config = BackendConfig(
                        name=backend.get("name") or f"pool_backend_{index}",
                        provider=backend.get("provider") or LLMFactory.DEFAULT_PROVIDER,
                        model_name=backend.get("model_name", ""),
                        api_key=backend.get("api_key", ""),
                        endpoint_url=backend.get("endpoint_url") or LLMFactory.DEFAULT_ENDPOINT_URL,
                    )

                if not backend_config.model_name:
                    raise ValueError(f"REFINER_LLM_POOL_{index}_MODEL_NAME is empty")
                if not backend_config.api_key:
                    raise ValueError(f"REFINER_LLM_POOL_{index}_API_KEY is empty")

                models.append(
                    LLMFactory._build_single_backend_model(
                        provider=backend_config.provider,
                        model_name=backend_config.model_name,
                        api_key=backend_config.api_key,
                        endpoint_url=backend_config.endpoint_url,
                        timeout=timeout,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        disable_thinking=disable_thinking,
                        do_sample=do_sample,
                        backend_name=backend_config.name,
                    )
                )

            return ModelPoolChatModel(
                models,
                max_rounds=pool_max_rounds or runtime_config["max_rounds"],
                round_backoff_seconds=pool_backoff_seconds or runtime_config["round_backoff_seconds"],
                wall_timeout_seconds=timeout,
            )

        resolved_provider = provider or config["provider"]
        resolved_model_name = model_name or config["model_name"]
        resolved_api_key = api_key or config["api_key"]
        resolved_endpoint_url = endpoint_url or config["endpoint_url"]

        if not resolved_api_key:
            raise ValueError(
                "API key is required. Please set REFINER_LLM_API_KEY in .env file "
                "or provide it as a parameter."
            )

        if os.getenv("REFINER_LLM_WIRE_API", "").strip().lower() == "codex_responses":
            return CodexResponsesModel(
                model=resolved_model_name,
                api_key=resolved_api_key,
                base_url=resolved_endpoint_url,
                reasoning_effort=os.getenv("REFINER_LLM_REASONING_EFFORT", "xhigh"),
                timeout=timeout,
                max_output_tokens=max_tokens,
                codex_path=os.getenv("REFINER_CODEX_CLI_PATH"),
            )

        return LLMFactory._build_single_backend_model(
            provider=resolved_provider,
            model_name=resolved_model_name,
            api_key=resolved_api_key,
            endpoint_url=resolved_endpoint_url,
            timeout=timeout,
            temperature=temperature,
            max_tokens=max_tokens,
            disable_thinking=disable_thinking,
            do_sample=do_sample,
            backend_name="single_backend",
        )

    @staticmethod
    def validate_config() -> Tuple[bool, str]:
        try:
            LLMFactory.load_env()
            pool_config = LLMFactory.get_pool_config()
            if pool_config:
                for index, backend in enumerate(pool_config, 1):
                    if not backend.model_name:
                        return False, f"REFINER_LLM_POOL_{index}_MODEL_NAME is empty"
                    if not backend.api_key:
                        return False, f"REFINER_LLM_POOL_{index}_API_KEY is empty"
                return True, f"Configuration is valid with {len(pool_config)} pooled backends"

            config = LLMFactory.get_config()
            if not config["api_key"]:
                return False, "API key is empty"

            return True, "Configuration is valid"
        except Exception as exc:
            return False, f"Validation failed: {str(exc)}"
