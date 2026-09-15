"""Bounded LangChain tool calling without text-protocol or CLI fallbacks.

The campaign workflows own routing and persistence. This module owns only one
model/tool conversation; application tools run once, outside transport retries.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Sequence

from .llm_retry import call_with_gateway_retry
from .llm_timing import measure_llm_request
from .responses_stream import responses_streaming_enabled


class NativeToolConfigurationError(RuntimeError):
    """The configured model/transport cannot serve native tool calls."""


class NativeToolProtocolError(RuntimeError):
    """A provider returned an invalid native tool conversation."""


class NativeToolBudgetExceeded(NativeToolProtocolError):
    """A model requested more tools after its explicit final-answer turn."""


def is_tool_support_error(exc: Exception) -> bool:
    if isinstance(exc, (NativeToolConfigurationError, NotImplementedError)):
        return True
    message = str(exc).lower()
    return any(
        token in message for token in ("tools", "tool calling", "function calling", "tool_choice")
    ) and any(
        token in message
        for token in ("not supported", "unsupported", "not support", "unknown parameter", "unrecognized")
    )


def require_direct_tool_transport() -> None:
    if os.getenv("REFINER_RESPONSES_TRANSPORT", "direct").strip().lower() in {"cli", "codex_cli"}:
        raise NativeToolConfigurationError(
            "Native tools require direct API transport; REFINER_RESPONSES_TRANSPORT=cli "
            "is text-only. No CLI or tool_request fallback is permitted."
        )


def make_native_openai_model(
    *,
    model: str,
    api_key: str,
    base_url: str,
    timeout: float,
    max_tokens: int | None = None,
    max_retries: int = 0,
    use_responses_api: bool = False,
    reasoning_effort: str | None = None,
    temperature: float | None = None,
) -> Any:
    """Delegate wire/message conversion to the installed LangChain provider."""
    if use_responses_api:
        require_direct_tool_transport()
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise NativeToolConfigurationError("Native tools require langchain-openai") from exc
    options: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
        "timeout": timeout,
        # Retried by ``invoke_with_tools`` with Chem Agent's fixed delay.
        "max_retries": 0,
        "use_responses_api": use_responses_api,
    }
    if max_tokens is not None:
        options["max_tokens"] = max_tokens
    if use_responses_api:
        options.update(
            store=False,
            use_previous_response_id=False,
            streaming=responses_streaming_enabled(),
        )
        if reasoning_effort:
            options["reasoning_effort"] = reasoning_effort
    elif temperature is not None:
        options["temperature"] = temperature
    native_model = ChatOpenAI(**options)
    object.__setattr__(native_model, "_chem_gateway_max_retries", max(0, int(max_retries)))
    return native_model


def _model_gateway_retry_budget(model: Any) -> int:
    """Read only an explicitly numeric retry budget from a model adapter."""

    for attribute in ("_transport_max_retries", "_chem_gateway_max_retries"):
        value = getattr(model, attribute, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
        if isinstance(value, str):
            try:
                return max(0, int(value))
            except ValueError:
                continue
    return 0


def invoke_with_tools(
    model: Any,
    messages: Sequence[Any],
    tools: Sequence[Any],
    *,
    max_rounds: int = 2,
    on_tool_result: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
) -> Any:
    """Run a native conversation with at most ``max_rounds`` tool executions.

    Parallel calls consume the same per-task budget individually. All calls get
    a matching ToolMessage, including invalid or over-budget requests. After the
    budget is spent the model gets one tools-disabled final-answer turn.
    """
    try:
        from langchain_core.messages import AIMessage, ToolMessage, convert_to_messages
    except ImportError as exc:
        raise NativeToolConfigurationError("Native tools require langchain-core") from exc

    if not callable(getattr(model, "bind_tools", None)):
        raise NativeToolConfigurationError(
            f"{type(model).__name__} does not support bind_tools; configure a native tool-calling model."
        )
    registry = {tool.name: tool for tool in tools}
    if len(registry) != len(tools):
        raise ValueError("Tool names must be unique")
    budget = max(0, int(max_rounds))
    history = list(convert_to_messages(list(messages)))
    seen_ids: set[str] = set()
    executions = 0
    # Invalid/unknown calls also consume budget so they cannot create a loop.
    attempts = 0

    def bind(final: bool = False) -> Any:
        try:
            return model.bind_tools(list(tools), tool_choice="none" if final else "auto")
        except (NotImplementedError, AttributeError, TypeError) as exc:
            raise NativeToolConfigurationError(
                f"{type(model).__name__} cannot bind native tools: {type(exc).__name__}"
            ) from exc

    bound = bind(final=budget == 0)
    for _ in range(budget + 1):
        final_turn = attempts >= budget
        try:
            def invoke_bound() -> Any:
                # Composite native pools time each concrete backend request.
                # Wrapping the whole pool here would merge retries and their
                # 10-second waits into one misleading success event.
                if getattr(bound, "handles_request_timing", False):
                    return bound.invoke(history)
                with measure_llm_request(
                    component=os.getenv("CHEM_LLM_COMPONENT", "native_tool"),
                    model=str(
                        getattr(model, "model_name", "")
                        or getattr(model, "_model", "")
                        or type(model).__name__
                    ),
                    transport="responses_native_tools",
                ):
                    return bound.invoke(history)

            response = call_with_gateway_retry(
                invoke_bound,
                max_retries=_model_gateway_retry_budget(model),
                operation_name="Native-tool gateway request",
            )
        except Exception as exc:
            if is_tool_support_error(exc):
                raise NativeToolConfigurationError(
                    "The configured endpoint rejects native tools; no text/CLI fallback was used."
                ) from exc
            raise
        if not isinstance(response, AIMessage):
            # Lightweight test/provider wrappers may carry the same native fields.
            response = AIMessage(
                content=getattr(response, "content", ""),
                tool_calls=list(getattr(response, "tool_calls", []) or []),
                invalid_tool_calls=list(getattr(response, "invalid_tool_calls", []) or []),
            )
        calls = list(response.tool_calls or [])
        invalid_calls = list(response.invalid_tool_calls or [])
        if not calls and not invalid_calls:
            return response
        if final_turn:
            raise NativeToolBudgetExceeded("Model requested tools after the tool budget was exhausted")
        history.append(response)
        for call in calls + invalid_calls:
            call_id = str(call.get("id") or "")
            if not call_id or call_id in seen_ids:
                raise NativeToolProtocolError("Native tool calls require unique, nonempty call IDs")
            seen_ids.add(call_id)
            name = str(call.get("name") or "")
            args = call.get("args")
            request = {"name": name, "args": args, "id": call_id}
            if attempts >= budget:
                output: dict[str, Any] = {"status": "error", "error": "tool_budget_exhausted"}
            elif not isinstance(args, dict):
                output = {"status": "error", "error": "invalid_tool_arguments", "detail": "Expected a JSON object"}
            elif name not in registry:
                output = {"status": "error", "error": "unknown_tool"}
            else:
                try:
                    value = registry[name].invoke(args)
                    executions += 1
                    output = value if isinstance(value, dict) else {"status": "ok", "result": value}
                except NativeToolConfigurationError:
                    raise
                except Exception as exc:
                    output = {
                        "status": "error", "error": type(exc).__name__, "detail": str(exc)[:1500]
                    }
            attempts += 1
            if on_tool_result is not None:
                on_tool_result(request, output)
            history.append(ToolMessage(
                content=json.dumps(output, ensure_ascii=False, default=str),
                tool_call_id=call_id,
                name=name,
                status="error" if output.get("status") == "error" else "success",
            ))
        if attempts >= budget:
            bound = bind(final=True)
    raise NativeToolBudgetExceeded(f"Native tool budget exhausted after {executions} executions")
