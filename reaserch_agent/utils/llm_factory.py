"""LLM factory used by the research agent."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional


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
        }
        if provider_url:
            model_kwargs["base_url"] = provider_url
        model_kwargs.update(kwargs)
        return ChatOpenAI(**model_kwargs)

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
