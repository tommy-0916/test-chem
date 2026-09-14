"""LLM factory used by the research agent."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from openai import OpenAI

from agent_skills.llm_retry import (
    RetryableGatewayError,
    call_with_gateway_retry,
    is_retryable_gateway_error,
)
from agent_skills.llm_timing import measure_llm_request


logger = logging.getLogger(__name__)


DEFAULT_LLM_MAX_RETRIES = 8


class _SingleAttemptGeminiClient:
    """Disable hidden GAPIC retries and sanitize transient Gemini failures."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def generate_content(self, *args: Any, **kwargs: Any) -> Any:
        # The Google client otherwise owns another retry loop beneath
        # LangChain.  Its ResourceExhausted handler can also sleep for the
        # provider's retry_after value even when LangChain max_retries is 0.
        kwargs["retry"] = None
        try:
            return self._client.generate_content(*args, **kwargs)
        except Exception as exc:
            if not is_retryable_gateway_error(exc):
                raise
            raise RetryableGatewayError(
                "Gemini gateway request failed with a transient error "
                f"({type(exc).__name__})"
            ) from exc


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
    """Return an OpenAI-compatible API base URL with the ``/v1`` prefix.

    The OpenAI Python SDK treats ``base_url`` as the API root and appends
    ``/responses`` or ``/chat/completions`` itself.  Several compatible
    gateways expose those routes below ``/v1``; accepting a host-only value
    here would therefore send requests to the wrong path.  Keep callers that
    already provide ``/v1`` unchanged and preserve any URL query/fragment.
    """

    value = str(base_url or "").strip().rstrip("/")
    if not value:
        return value
    parsed = urlsplit(value)
    path = parsed.path.rstrip("/")
    if path == "/v1" or path.endswith("/v1"):
        return value
    path = f"{path}/v1" if path else "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)).rstrip("/")

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - optional local dependency
    def load_dotenv(path: Any = None, *args: Any, **kwargs: Any) -> bool:
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


def default_env_file() -> Path:
    if os.getenv("CHEM_AGENT_ENV_FILE"):
        return Path(os.environ["CHEM_AGENT_ENV_FILE"]).expanduser().resolve()

    repo_root = Path(__file__).resolve().parents[2]
    for candidate in (
        repo_root / ".env",
        Path("/workspace/.env"),
    ):
        if candidate.exists():
            return candidate
    return repo_root / ".env"


class CodexResponsesModel:
    """Stateless direct Responses adapter with an optional CLI fallback."""

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
        self._model = model
        self._api_key = api_key
        self._base_url = normalize_openai_base_url(base_url)
        self._reasoning_effort = reasoning_effort
        self._timeout = timeout
        self._max_output_tokens = max_output_tokens
        self._transport_max_retries = configured_max_retries()
        self.handles_transport_retries = True
        self._codex_path = codex_path or shutil.which("codex") or "/Applications/Codex.app/Contents/Resources/codex"
        self._client = client or OpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=self._timeout,
            # Retry explicitly so Retry-After: 60 cannot stall the campaign
            # and timing can distinguish every transport attempt.
            max_retries=0,
        )

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Native tools use direct Responses messages, never the text/CLI path."""
        from agent_skills.native_tools import make_native_openai_model, require_direct_tool_transport

        require_direct_tool_transport()
        if getattr(self, "_native_tool_model", None) is None:
            self._native_tool_model = make_native_openai_model(
                model=self._model,
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout,
                max_tokens=self._max_output_tokens,
                max_retries=configured_max_retries(),
                use_responses_api=True,
                reasoning_effort=self._reasoning_effort,
            )
        return self._native_tool_model.bind_tools(tools, **kwargs)

    def invoke(self, messages: Any) -> Any:
        prompt = self._messages_to_prompt(messages)
        transport = os.getenv("REFINER_RESPONSES_TRANSPORT", "direct").strip().lower()
        if transport not in {"cli", "codex_cli"}:
            try:
                return call_with_gateway_retry(
                    lambda: self._invoke_direct(prompt),
                    max_retries=self._transport_max_retries,
                    logger=logger,
                    operation_name="Research Responses request",
                )
            except Exception as exc:
                if is_retryable_gateway_error(exc):
                    # CLI targets the same provider and cannot repair an
                    # exhausted transient gateway failure.
                    raise
                fallback = os.getenv(
                    "REFINER_RESPONSES_CLI_FALLBACK", "1"
                ).strip().lower()
                if fallback in {"0", "off", "false", "no"}:
                    raise
                logger.exception(
                    "Direct Responses request failed; falling back to Codex CLI"
                )
        return call_with_gateway_retry(
            lambda: self._invoke_cli(prompt),
            max_retries=self._transport_max_retries,
            logger=logger,
            operation_name="Research Codex CLI request",
        )

    def _invoke_direct(self, prompt: str) -> Any:
        payload: Dict[str, Any] = {
            "model": self._model,
            "input": prompt,
            "store": False,
        }
        if self._reasoning_effort:
            payload["reasoning"] = {"effort": self._reasoning_effort}
        if self._max_output_tokens is not None:
            payload["max_output_tokens"] = self._max_output_tokens
        with measure_llm_request(
            component="research", model=self._model, transport="responses"
        ):
            response = self._client.responses.create(**payload)
            text = self._extract_response_text(response)
            if not text:
                status = getattr(response, "status", "")
                raise RuntimeError(
                    f"Responses API returned no text output (status={status or 'unknown'})"
                )
            return SimpleNamespace(content=text, raw_response=response)

    def _invoke_cli(self, prompt: str) -> Any:
        tmpdir = tempfile.mkdtemp(prefix="research-codex-")
        try:
            tmp_path = Path(tmpdir)
            output_path = tmp_path / "last_message.txt"
            self._write_codex_home(tmp_path)
            stateless_prompt = (
                "You are serving one stateless structured-inference request. "
                "All evidence needed to answer is already included below. Do not call "
                "tools, inspect files, browse the network, or discuss your work. Return "
                "the requested final JSON/text directly.\n\n"
                + prompt
            )
            cmd = [
                self._codex_path,
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--output-last-message",
                str(output_path),
                "--json",
                "-",
            ]
            env = dict(os.environ)
            env["CODEX_HOME"] = str(tmp_path)
            # Codex authenticates from the private auth.json in CODEX_HOME.
            # Do not also expose credentials through the child environment,
            # because interrupted Codex sessions may persist shell snapshots.
            env.pop("OPENAI_API_KEY", None)
            env.pop("REFINER_LLM_API_KEY", None)
            with measure_llm_request(
                component="research", model=self._model, transport="codex_cli"
            ):
                completed = subprocess.run(
                    cmd,
                    input=stateless_prompt,
                    text=True,
                    capture_output=True,
                    timeout=self._timeout,
                    env=env,
                    cwd=tmp_path,
                    check=False,
                )
                if completed.returncode != 0:
                    diagnostic = self._cli_failure_diagnostic(completed)
                    if self._is_retryable_cli_failure(completed):
                        raise RetryableGatewayError(diagnostic)
                    raise RuntimeError(diagnostic)
            if not output_path.exists():
                raise RuntimeError(
                    "Codex responses call did not produce output-last-message"
                )
            text = output_path.read_text(encoding="utf-8").strip()
            if not text:
                raise RuntimeError("Codex responses call returned empty output")
            return SimpleNamespace(content=text)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @classmethod
    def _is_retryable_cli_failure(
        cls,
        completed: subprocess.CompletedProcess[str],
    ) -> bool:
        """Classify private CLI output, then discard it at the boundary."""

        detail = f"{completed.stdout or ''}\n{completed.stderr or ''}".lower()
        patterns = (
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
            for pattern in patterns
            for match in pattern.finditer(detail)
        ]
        if matches:
            status = int(max(matches, key=lambda match: match.start()).group(1))
            return status in {408, 409, 425, 429} or 500 <= status <= 599
        # The shared classifier covers connection, timeout and protocol
        # markers. It sees this text only inside the current process.
        return is_retryable_gateway_error(RuntimeError(detail))

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
        parts = []
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
model = "{self._escape_toml(self._model)}"
model_reasoning_effort = "{self._escape_toml(self._reasoning_effort)}"
disable_response_storage = true
network_access = "enabled"
model_context_window = 1000000
model_auto_compact_token_limit = 900000

[model_providers.OpenAI]
name = "OpenAI"
base_url = "{self._escape_toml(self._base_url)}"
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
            json.dumps({"OPENAI_API_KEY": self._api_key}, ensure_ascii=False),
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        auth_path.chmod(0o600)

    def _messages_to_prompt(self, messages: Any) -> str:
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


class LLMFactory:
    """Create a chat model if credentials are available, otherwise return None."""

    @staticmethod
    def load_env(env_path: Optional[str] = None) -> None:
        load_dotenv(env_path or default_env_file())

    @staticmethod
    def create_or_none(
        model_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        # Issue 3 (repeatability): default to fully deterministic sampling so
        # identical queries against an identical KB reproduce identical plans
        # as far as the backend allows. Matches the device layer's default.
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> Any:
        LLMFactory.load_env()
        provider_model = (
            model_name
            or os.getenv("REFINER_LLM_MODEL_NAME")
            or os.getenv("GEMINI_MODEL")
            or os.getenv("OPENAI_MODEL")
        )
        provider_key = (
            api_key
            or os.getenv("REFINER_LLM_API_KEY")
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        provider_url = (
            base_url
            or os.getenv("REFINER_LLM_ENDPOINT_URL")
            or os.getenv("GEMINI_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
        )

        if not provider_model or not provider_key:
            if provider_url:
                provider_model = LLMFactory._extract_gemini_model_from_url(provider_url)
            if not provider_model or not provider_key:
                return None

        if LLMFactory._looks_like_gemini(provider_model, provider_url, provider_key):
            return LLMFactory._create_gemini_model(
                provider_model=provider_model,
                provider_key=provider_key,
                provider_url=provider_url,
                temperature=temperature,
                **kwargs,
            )

        if os.getenv("REFINER_LLM_WIRE_API", "").strip().lower() == "codex_responses":
            if not provider_url:
                return None
            return CodexResponsesModel(
                model=provider_model,
                api_key=provider_key,
                base_url=provider_url,
                reasoning_effort=os.getenv("REFINER_LLM_REASONING_EFFORT", "xhigh"),
                timeout=LLMFactory._openai_compatible_timeout(),
                max_output_tokens=int(
                    os.getenv("REFINER_LLM_RESPONSES_MAX_OUTPUT_TOKENS", "32768")
                ),
                codex_path=os.getenv("REFINER_CODEX_CLI_PATH"),
            )

        return LLMFactory._create_openai_compatible_model(
            provider_model=provider_model,
            provider_key=provider_key,
            provider_url=provider_url,
            temperature=temperature,
            **kwargs,
        )

    @staticmethod
    def _create_openai_compatible_model(
        provider_model: str,
        provider_key: str,
        provider_url: Optional[str],
        temperature: float,
        **kwargs: Any,
    ) -> Any:
        try:
            from langchain_openai import ChatOpenAI
        except Exception:
            return None

        model_kwargs: Dict[str, Any] = {
            "model": provider_model,
            "api_key": provider_key,
            "temperature": temperature,
            "default_headers": LLMFactory._openai_compatible_headers(),
            "timeout": LLMFactory._openai_compatible_timeout(),
            # BaseAgent/native_tools own retries so Retry-After: 60 cannot be
            # honored invisibly inside the SDK.
            "max_retries": 0,
            "use_responses_api": False,
        }
        if provider_url:
            model_kwargs["base_url"] = normalize_openai_base_url(provider_url)
        model_kwargs.update(kwargs)
        # A caller-provided LangChain option must not re-enable the SDK's
        # Retry-After-aware retry loop.
        model_kwargs["max_retries"] = 0
        model = ChatOpenAI(**model_kwargs)
        object.__setattr__(
            model,
            "_chem_gateway_max_retries",
            configured_max_retries(),
        )
        return model

    @staticmethod
    def _openai_compatible_timeout() -> float:
        raw_timeout = os.getenv("REFINER_LLM_TIMEOUT_SECONDS", "60")
        try:
            return max(1.0, float(raw_timeout))
        except ValueError:
            return 60.0

    @staticmethod
    def _openai_compatible_headers() -> Dict[str, str]:
        """Headers for OpenAI-compatible gateways that front chat/completions."""
        headers: Dict[str, str] = {
            "User-Agent": os.getenv("REFINER_LLM_USER_AGENT", "OpenAI/NodeJS/4.0"),
        }
        raw_headers = os.getenv("REFINER_LLM_DEFAULT_HEADERS")
        if raw_headers:
            try:
                parsed_headers = json.loads(raw_headers)
                if isinstance(parsed_headers, dict):
                    headers.update(
                        {str(key): str(value) for key, value in parsed_headers.items()}
                    )
            except json.JSONDecodeError:
                # Keep startup forgiving; invalid optional headers should not disable the LLM.
                pass
        return headers

    @staticmethod
    def _create_gemini_model(
        provider_model: str,
        provider_key: str,
        provider_url: Optional[str],
        temperature: float,
        **kwargs: Any,
    ) -> Any:
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except Exception:
            return None

        model_kwargs: Dict[str, Any] = {
            "model": provider_model,
            "api_key": provider_key,
            "temperature": temperature,
        }
        client_options = LLMFactory._extract_gemini_client_options(provider_url)
        if client_options:
            model_kwargs["client_options"] = client_options
        model_kwargs.update(kwargs)
        model_kwargs["max_retries"] = 0
        model = ChatGoogleGenerativeAI(**model_kwargs)
        client = getattr(model, "client", None)
        if client is not None:
            object.__setattr__(model, "client", _SingleAttemptGeminiClient(client))
        object.__setattr__(
            model,
            "_chem_gateway_max_retries",
            configured_max_retries(),
        )
        return model

    @staticmethod
    def _looks_like_gemini(
        provider_model: Optional[str],
        provider_url: Optional[str],
        provider_key: Optional[str],
    ) -> bool:
        model_hint = (provider_model or "").lower()
        url_hint = (provider_url or "").lower()
        key_hint = provider_key or ""
        return (
            model_hint.startswith("gemini")
            or "generativelanguage.googleapis.com" in url_hint
            or key_hint.startswith("AIza")
        )

    @staticmethod
    def _extract_gemini_model_from_url(provider_url: str) -> Optional[str]:
        match = re.search(r"/models/([^:/]+):generateContent", provider_url)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _extract_gemini_client_options(provider_url: Optional[str]) -> Optional[Dict[str, str]]:
        if not provider_url or "://" not in provider_url:
            return None
        # The LangChain Gemini client expects the API host, not the full REST path.
        if "generativelanguage.googleapis.com" in provider_url:
            return {"api_endpoint": "generativelanguage.googleapis.com"}

        try:
            host = provider_url.split("://", 1)[1].split("/", 1)[0]
        except Exception:
            return None
        if not host:
            return None
        return {"api_endpoint": host}
