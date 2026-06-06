"""
LLM instance factory and pooled backend runtime.
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from openai import OpenAI
from utils.paths import default_env_file

logger = logging.getLogger(__name__)


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


class OpenAICompatChatModel:
    """Minimal OpenAI-compatible chat model wrapper for one backend."""

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
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.disable_thinking = disable_thinking
        self.do_sample = do_sample
        self.name = name or f"{self.base_url}:{model}"
        self._client = OpenAI(api_key=api_key, base_url=self.base_url)
        self._supports_extra_body: Optional[bool] = None

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
            "timeout": self.timeout,
            "temperature": self.temperature,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens

        if include_extra_body:
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
        last_error: Optional[Exception] = None

        for model_name in self._candidate_models():
            for payload_label, payload in self._payload_variants(messages):
                try:
                    response = self._client.chat.completions.create(model=model_name, **payload)
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
                            "LLM backend %s recovered by retrying model %s without extra_body",
                            self.name,
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
                                "LLM backend %s detected deterministic extra_body rejection on model %s; "
                                "future calls will skip compat payloads for this backend",
                                self.name,
                                model_name,
                            )
                        self._supports_extra_body = False
                    logger.warning(
                        "LLM backend %s model %s failed with %s payload: %s: %s",
                        self.name,
                        model_name,
                        payload_label,
                        type(exc).__name__,
                        exc,
                    )
                    continue

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"No available model candidates for backend {self.name}")


class ModelPoolChatModel:
    """Pooled model wrapper that fails over across multiple backends."""

    def __init__(
        self,
        backends: List[OpenAICompatChatModel],
        *,
        max_rounds: int = 2,
        round_backoff_seconds: Optional[List[int]] = None,
    ) -> None:
        if not backends:
            raise ValueError("ModelPoolChatModel requires at least one backend")
        self.backends = backends
        self.max_rounds = max(1, max_rounds)
        self.round_backoff_seconds = round_backoff_seconds or [2, 5]
        self._preferred_backend_index = 0

    def _ordered_backend_entries(self) -> List[Tuple[int, OpenAICompatChatModel]]:
        if len(self.backends) == 1:
            return [(0, self.backends[0])]

        ordered: List[Tuple[int, OpenAICompatChatModel]] = []
        start = self._preferred_backend_index % len(self.backends)
        for offset in range(len(self.backends)):
            index = (start + offset) % len(self.backends)
            ordered.append((index, self.backends[index]))
        return ordered

    def invoke(self, messages: List[Any]) -> ChatResponse:
        last_error: Optional[Exception] = None
        failure_notes: List[str] = []

        for round_index in range(self.max_rounds):
            for backend_index, backend in self._ordered_backend_entries():
                try:
                    response = backend.invoke(messages)
                    if backend_index != self._preferred_backend_index:
                        logger.warning(
                            "LLM backend failover activated: %s -> %s",
                            self.backends[self._preferred_backend_index].name,
                            backend.name,
                        )
                    self._preferred_backend_index = backend_index
                    return response
                except Exception as exc:
                    last_error = exc
                    failure_notes.append(
                        "round {round_num}/{round_total} backend {backend_name}: {exc_type}: {exc_msg}".format(
                            round_num=round_index + 1,
                            round_total=self.max_rounds,
                            backend_name=backend.name,
                            exc_type=type(exc).__name__,
                            exc_msg=exc,
                        )
                    )
                    logger.warning(
                        "LLM backend %s failed in pool round %s/%s: %s: %s",
                        backend.name,
                        round_index + 1,
                        self.max_rounds,
                        type(exc).__name__,
                        exc,
                    )

            if round_index < self.max_rounds - 1:
                delay = self.round_backoff_seconds[min(round_index, len(self.round_backoff_seconds) - 1)]
                logger.warning(
                    "All LLM backends failed in pool round %s/%s, sleeping %ss before retrying the pool",
                    round_index + 1,
                    self.max_rounds,
                    delay,
                )
                time.sleep(delay)

        recent_failures = " | ".join(failure_notes[-6:])
        message = (
            f"All configured LLM backends failed after {self.max_rounds} pool round(s). "
            f"Recent failures: {recent_failures}"
        )
        if last_error is not None:
            raise RuntimeError(message) from last_error
        raise RuntimeError(message)


class LLMFactory:
    """Create single-backend or pooled OpenAI-compatible chat models."""

    DEFAULT_PROVIDER = "openai"
    DEFAULT_MODEL_NAME = "qwen3-vl-plus"
    DEFAULT_ENDPOINT_URL = "https://apis.iflow.cn/v1"
    DEFAULT_TIMEOUT_SECONDS = 360
    DEFAULT_TEMPERATURE = 0
    DEFAULT_MAX_TOKENS = 3072
    DEFAULT_POOL_BACKOFF_SECONDS = [2, 5]
    DEFAULT_POOL_MAX_ROUNDS = 2

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
                os.getenv("REFINER_LLM_POOL_MAX_ROUNDS", str(LLMFactory.DEFAULT_POOL_MAX_ROUNDS))
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
