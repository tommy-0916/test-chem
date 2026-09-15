"""Bounded LangChain tool calling without text-protocol or CLI fallbacks.

The campaign workflows own routing and persistence. This module owns only one
model/tool conversation; application tools run once, outside transport retries.
"""

from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from functools import lru_cache
from typing import Any, Callable, ClassVar, Sequence

from agent_skills.responses_diagnostics import get_responses_diagnostics
from agent_skills.llm_retry import (
    NonRetryableGatewayError,
    call_with_gateway_retry,
    configured_gateway_retry_delay_seconds,
    configured_wall_timeout,
    current_attempt,
    is_retryable_gateway_error,
)
from agent_skills.llm_timing import measure_llm_request
from agent_skills.responses_stream import (
    ResponsesTerminalError,
    configured_responses_streaming,
    invoke_responses,
    invoke_responses_async,
)


class NativeToolConfigurationError(RuntimeError):
    """The configured model/transport cannot serve native tool calls."""


class NativeToolProtocolError(NonRetryableGatewayError):
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


@lru_cache(maxsize=1)
def _validated_responses_model_type() -> type:
    """Keep LangChain's wire compiler, but release only a validated terminal.

    langchain-openai 1.1.7's event converter accepts response.incomplete and
    builds tool arguments from deltas rather than the authoritative completed
    response. Merely setting streaming=True therefore cannot enforce our tool
    execution boundary. These hooks stream at the SDK transport, validate the
    complete lifecycle, then use the provider's normal full-response decoder.
    No partial token/tool callback is emitted before clean SDK-decoded stream
    completion. The SDK hides raw bytes after its [DONE]/EOF boundary; this
    layer does not claim to inspect an HTTP tail the SDK no longer exposes.
    """
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk
    from langchain_openai import ChatOpenAI
    from langchain_openai.chat_models.base import _construct_lc_result_from_responses_api

    class ValidatedResponsesChatOpenAI(ChatOpenAI):
        handles_request_timing: ClassVar[bool] = True

        def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
            self._ensure_sync_client_available()
            payload = self._get_request_payload(messages, stop=stop, **kwargs)
            with measure_llm_request(
                component=os.getenv("CHEM_LLM_COMPONENT", "native_tool"),
                model=str(self.model_name),
                transport="responses_native_tools",
            ) as timing:
                response = invoke_responses(
                    self.root_client, payload, on_event=timing.observe,
                )
            return _construct_lc_result_from_responses_api(
                response, schema=kwargs.get("response_format"), output_version=self.output_version,
            )

        async def _agenerate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
            payload = self._get_request_payload(messages, stop=stop, **kwargs)
            with measure_llm_request(
                component=os.getenv("CHEM_LLM_COMPONENT", "native_tool"),
                model=str(self.model_name),
                transport="responses_native_tools",
            ) as timing:
                response = await invoke_responses_async(
                    self.root_async_client, payload, on_event=timing.observe,
                )
            return _construct_lc_result_from_responses_api(
                response, schema=kwargs.get("response_format"), output_version=self.output_version,
            )

        @staticmethod
        def _final_chunk(result: Any) -> Any:
            generation = result.generations[0]
            # Preserve the provider-decoded message, including response/item
            # IDs, reasoning blocks, tool-call IDs and usage. Pydantic fills
            # tool_call_chunks from the already-complete native tool calls.
            message = AIMessageChunk(
                **generation.message.model_dump(exclude={"type"}),
                chunk_position="last",
            )
            return ChatGenerationChunk(message=message, generation_info=generation.generation_info)

        def _stream(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
            chunk = self._final_chunk(self._generate(messages, stop=stop, **kwargs))
            if run_manager:
                run_manager.on_llm_new_token(chunk.text, chunk=chunk)
            yield chunk

        async def _astream(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
            chunk = self._final_chunk(await self._agenerate(messages, stop=stop, **kwargs))
            if run_manager:
                await run_manager.on_llm_new_token(chunk.text, chunk=chunk)
            yield chunk

    return ValidatedResponsesChatOpenAI


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
        # Application middleware owns any scoped replay.  Leaving SDK retries
        # enabled here multiplies the Device retry, proxy retry and pool retry.
        "max_retries": 0,
        "use_responses_api": use_responses_api,
    }
    if max_tokens is not None:
        options["max_tokens"] = max_tokens
    if use_responses_api:
        options.update(store=False, use_previous_response_id=False, streaming=configured_responses_streaming())
        if reasoning_effort:
            options["reasoning_effort"] = reasoning_effort
    elif temperature is not None:
        options["temperature"] = temperature
    provider_type = _validated_responses_model_type() if use_responses_api else ChatOpenAI
    native_model = provider_type(**options)
    object.__setattr__(native_model, "_chem_gateway_max_retries", max(0, int(max_retries)))
    object.__setattr__(
        native_model,
        "_chem_wall_timeout_seconds",
        configured_wall_timeout(timeout),
    )
    return native_model


def _model_gateway_retry_budget(model: Any) -> int:
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


def _model_wall_timeout(model: Any) -> float | None:
    for attribute in ("_chem_wall_timeout_seconds", "wall_timeout_seconds", "timeout"):
        value = getattr(model, attribute, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _bound_handles_request_timing(bound: Any) -> bool:
    underlying = getattr(bound, "bound", None)
    return any(
        getattr(candidate, "handles_request_timing", False) is True
        for candidate in (bound, underlying)
        if candidate is not None
    )


def invoke_with_tools(
    model: Any,
    messages: Sequence[Any],
    tools: Sequence[Any],
    *,
    max_rounds: int = 2,
    on_tool_result: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    retry_upstream_errors: bool = False,
    max_upstream_retries: int = 1,
    on_model_attempt: Callable[[dict[str, Any]], None] | None = None,
) -> Any:
    """Run a native conversation with at most ``max_rounds`` tool executions.

    Parallel calls consume the same per-task budget individually. All calls get
    a matching ToolMessage, including invalid or over-budget requests. After the
    budget is spent the model gets one tools-disabled final-answer turn.

    An explicit opt-in caps shared transport retries at
    ``max_upstream_retries`` (0 through 3) across this whole conversation.
    Without that override the model's shared retry budget is used. A retry
    covers transient network/5xx/429 and Responses ``upstream_error`` failures,
    and resends the exact last
    completed conversation, including existing tool results; it never replays
    tools or consumes their budget. No failed response is added to history.
    The provider's complete-response/EOF validation is unchanged.

    ``on_model_attempt`` receives metadata-only start/completed/failed events
    with 1-based model-turn and attempt numbers. Failed events include sanitized
    Responses diagnostics and whether a retry is scheduled. Callback exceptions
    propagate immediately, before retrying or executing any subsequent tools.
    """
    if type(retry_upstream_errors) is not bool:
        raise ValueError("retry_upstream_errors must be a boolean")
    if type(max_upstream_retries) is not int or not 0 <= max_upstream_retries <= 3:
        raise ValueError("max_upstream_retries must be an integer from 0 through 3")
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
    upstream_retry_budget = (
        max_upstream_retries
        if retry_upstream_errors
        else _model_gateway_retry_budget(model)
    )
    upstream_retries_used = 0

    def bind(final: bool = False) -> Any:
        try:
            return model.bind_tools(list(tools), tool_choice="none" if final else "auto")
        except (NotImplementedError, AttributeError, TypeError) as exc:
            raise NativeToolConfigurationError(
                f"{type(model).__name__} cannot bind native tools: {type(exc).__name__}"
            ) from exc

    bound = bind(final=budget == 0)
    model_name = str(
        getattr(model, "model_name", "")
        or getattr(model, "model", "")
        or type(model).__name__
    )

    def emit_model_attempt(event: dict[str, Any]) -> None:
        if on_model_attempt is None:
            return
        try:
            on_model_attempt(event)
        except Exception as callback_error:
            # A persistence/observer failure is application-local. If it was
            # raised while handling a retryable provider exception, Python
            # links that provider error through __context__; mark the callback
            # explicitly so the transport loop never mistakes it for a reason
            # to send another model request.
            try:
                setattr(callback_error, "_chem_no_retry", True)
            except Exception:
                pass
            raise

    for model_turn in range(1, budget + 2):
        final_turn = attempts >= budget
        model_attempt = 0
        remaining_retries = max(0, upstream_retry_budget - upstream_retries_used)

        def invoke_model_attempt() -> Any:
            nonlocal model_attempt, upstream_retries_used
            model_attempt += 1
            attempt_metadata = {
                "model_turn": model_turn,
                "attempt": model_attempt,
                "tools_disabled": final_turn,
                "upstream_retries_used": upstream_retries_used,
                "max_upstream_retries": upstream_retry_budget,
            }
            # Preserve an unchanged checkpoint even if a custom wrapper
            # mutates its input before raising. Default callers retain the
            # existing invocation behavior and avoid the extra copy.
            attempt_history = deepcopy(history) if remaining_retries else history
            emit_model_attempt({"event": "model_attempt_start", **attempt_metadata})
            try:
                if _bound_handles_request_timing(bound):
                    response = bound.invoke(attempt_history)
                else:
                    with measure_llm_request(
                        component=os.getenv("CHEM_LLM_COMPONENT", "native_tool"),
                        model=model_name,
                        transport=(
                            "responses_native_tools"
                            if getattr(model, "use_responses_api", False)
                            else "chat_native_tools"
                        ),
                    ):
                        response = bound.invoke(attempt_history)
            except Exception as exc:
                diagnostics = get_responses_diagnostics(exc)
                active = current_attempt()
                enough_time = (
                    active is None
                    or active.deadline - time.monotonic()
                    > configured_gateway_retry_delay_seconds()
                )
                retry_scheduled = (
                    is_retryable_gateway_error(exc)
                    and upstream_retries_used < upstream_retry_budget
                    and enough_time
                )
                emit_model_attempt({
                    "event": "model_attempt_failed", **attempt_metadata,
                    "exception_type": type(exc).__name__,
                    "retry_scheduled": retry_scheduled,
                    **({"responses_diagnostics": diagnostics} if diagnostics is not None else {}),
                })
                if retry_scheduled:
                    upstream_retries_used += 1
                if is_tool_support_error(exc):
                    raise NativeToolConfigurationError(
                        "The configured endpoint rejects native tools; no text/CLI fallback was used."
                    ) from exc
                raise
            emit_model_attempt(
                {"event": "model_attempt_completed", **attempt_metadata}
            )
            return response

        if getattr(bound, "handles_transport_retries", False) is True:
            response = invoke_model_attempt()
        else:
            response = call_with_gateway_retry(
                invoke_model_attempt,
                max_retries=remaining_retries,
                wall_timeout_seconds=_model_wall_timeout(model),
                operation_name="Native-tool gateway request",
            )
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
