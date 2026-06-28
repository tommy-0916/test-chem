"""LLM factory used by the research agent."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional


class CodexResponsesModel:
    """Small adapter for Codex CLI providers that require wire_api=responses."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        reasoning_effort: str = "xhigh",
        timeout: float = 180.0,
        codex_path: Optional[str] = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._reasoning_effort = reasoning_effort
        self._timeout = timeout
        self._codex_path = codex_path or shutil.which("codex") or "/Applications/Codex.app/Contents/Resources/codex"

    def invoke(self, messages: Any) -> Any:
        prompt = self._messages_to_prompt(messages)
        tmpdir = tempfile.mkdtemp(prefix="research-codex-")
        try:
            tmp_path = Path(tmpdir)
            output_path = tmp_path / "last_message.txt"
            self._write_codex_home(tmp_path)
            cmd = [
                self._codex_path,
                "exec",
                "--skip-git-repo-check",
                "--output-last-message",
                str(output_path),
                "--json",
                "-",
            ]
            env = dict(os.environ)
            env["CODEX_HOME"] = str(tmp_path)
            env["OPENAI_API_KEY"] = self._api_key
            completed = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self._timeout,
                env=env,
                check=False,
            )
            if completed.returncode != 0:
                stderr = completed.stderr.strip()
                stdout = completed.stdout.strip()
                detail = stderr or stdout or f"exit code {completed.returncode}"
                raise RuntimeError(f"Codex responses call failed: {detail[-2000:]}")
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
"""
        if bundled_marketplace.exists():
            config += f"""
[marketplaces.openai-bundled]
source_type = "local"
source = "{self._escape_toml(str(bundled_marketplace))}"
"""
        if primary_runtime_marketplace.exists():
            config += f"""
[marketplaces.openai-primary-runtime]
source_type = "local"
source = "{self._escape_toml(str(primary_runtime_marketplace))}"
"""
        (path / "config.toml").write_text(config, encoding="utf-8")
        (path / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": self._api_key}, ensure_ascii=False),
            encoding="utf-8",
        )

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
    def create_or_none(
        model_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.1,
        **kwargs: Any,
    ) -> Any:
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
            "use_responses_api": False,
        }
        if provider_url:
            model_kwargs["base_url"] = provider_url
        model_kwargs.update(kwargs)
        return ChatOpenAI(**model_kwargs)

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
        return ChatGoogleGenerativeAI(**model_kwargs)

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
