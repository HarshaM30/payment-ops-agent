"""The only way the application talks to an LLM.

``LLMClient`` adds, around a provider backend (LiteLLM, or the fake one):

- client-side throttling (requests/min and tokens/min),
- retries with backoff + jitter on 429/5xx/timeouts, honoring ``Retry-After``,
- a hard timeout per call,
- structured output: ask for JSON, validate against a Pydantic schema, send one
  corrective re-prompt on failure, then fail cleanly with ``LLMSchemaError``,
- token usage and latency for every call, passed to an optional ``recorder`` so the
  agent can write each call to ``audit_log``.
"""

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from payops.config import Settings, get_settings
from payops.llm.models import litellm_model, resolve_model
from payops.llm.retry import LLMError, RetryPolicy, RetryStats, call_with_retry
from payops.llm.throttle import Throttle, estimate_tokens
from payops.services.audit import TokenUsage

M = TypeVar("M", bound=BaseModel)
Message = dict[str, Any]


@dataclass
class LLMRequest:
    node: str
    messages: list[Message]
    tools: list[dict[str, Any]] | None = None
    json_mode: bool = False
    # Structured facts behind the prompt. Real providers ignore this (the same facts are
    # rendered into the messages); the fake provider uses it to script responses.
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text, exactly as the model produced it

    def parsed_arguments(self) -> dict[str, Any]:
        value = json.loads(self.arguments or "{}")
        if not isinstance(value, dict):
            raise ValueError("tool arguments must be a JSON object")
        return value


@dataclass(frozen=True)
class Completion:
    """What a backend returns for one successful call."""

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass(frozen=True)
class LLMResponse(Completion):
    latency_ms: int = 0
    attempts: int = 1


@dataclass(frozen=True)
class LLMCallRecord:
    node: str
    purpose: str  # "call" | "corrective_reprompt"
    request: LLMRequest
    response: LLMResponse | None
    error: str | None
    latency_ms: int
    usage: TokenUsage | None


Recorder = Callable[[LLMCallRecord], None]


class Backend(Protocol):
    def complete(self, request: LLMRequest, *, timeout: float) -> Completion: ...


class LLMSchemaError(LLMError):
    """The model's output failed schema validation, even after a corrective re-prompt."""

    def __init__(self, message: str, errors: list[dict[str, Any]], raw: str | None) -> None:
        super().__init__(message)
        self.errors = errors
        self.raw = raw


@dataclass(frozen=True)
class StructuredResult[T]:
    value: T
    usage: TokenUsage
    latency_ms: int
    reprompted: bool


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def extract_json(text: str | None) -> Any:
    """Parse a JSON object from model text, tolerating code fences and leading prose."""
    if not text:
        raise ValueError("empty response")
    stripped = _FENCE.sub("", text.strip())
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in response") from None
        return json.loads(stripped[start : end + 1])


def _schema_errors(exc: Exception) -> list[dict[str, Any]]:
    if isinstance(exc, ValidationError):
        return [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    return [{"loc": [], "msg": str(exc), "type": "json_invalid"}]


def _add_usage(a: TokenUsage, b: TokenUsage) -> TokenUsage:
    return TokenUsage(a.prompt_tokens + b.prompt_tokens, a.completion_tokens + b.completion_tokens)


class LLMClient:
    def __init__(
        self,
        backend: Backend,
        *,
        provider: str,
        model: str,
        throttle: Throttle | None = None,
        retry_policy: RetryPolicy | None = None,
        timeout_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.backend = backend
        self.provider = provider
        self.model = model
        self.throttle = throttle
        self.retry_policy = retry_policy or RetryPolicy()
        self.timeout_seconds = timeout_seconds
        self._sleep = sleep

    # ------------------------------------------------------------------ raw calls

    def complete(
        self, request: LLMRequest, *, recorder: Recorder | None = None, purpose: str = "call"
    ) -> LLMResponse:
        estimated = estimate_tokens(json.dumps(request.messages, default=str))
        stats = RetryStats()
        start = time.perf_counter()

        def attempt() -> Completion:
            if self.throttle:
                self.throttle.acquire(estimated)
            return self.backend.complete(request, timeout=self.timeout_seconds)

        try:
            result = call_with_retry(attempt, self.retry_policy, sleep=self._sleep, stats=stats)
        except LLMError as exc:
            latency = round((time.perf_counter() - start) * 1000)
            if recorder:
                record = LLMCallRecord(
                    request.node, purpose, request, None, str(exc), latency, None
                )
                recorder(record)
            raise
        latency = round((time.perf_counter() - start) * 1000)
        if self.throttle:
            self.throttle.record_usage(estimated, result.usage.total_tokens)
        response = LLMResponse(
            content=result.content,
            tool_calls=list(result.tool_calls),
            usage=result.usage,
            latency_ms=latency,
            attempts=stats.attempts,
        )
        if recorder:
            recorder(
                LLMCallRecord(request.node, purpose, request, response, None, latency, result.usage)
            )
        return response

    # ------------------------------------------------------------------ structured output

    def structured(
        self, request: LLMRequest, schema: type[M], *, recorder: Recorder | None = None
    ) -> StructuredResult[M]:
        """Return a validated ``schema`` instance, with one corrective re-prompt on failure."""
        request.json_mode = True
        first = self.complete(request, recorder=recorder)
        try:
            value = schema.model_validate(extract_json(first.content))
            return StructuredResult(value, first.usage, first.latency_ms, reprompted=False)
        except (ValueError, ValidationError) as exc:
            errors = _schema_errors(exc)

        correction = LLMRequest(
            node=request.node,
            messages=[
                *request.messages,
                {"role": "assistant", "content": first.content or ""},
                {
                    "role": "user",
                    "content": (
                        "Your previous reply did not match the required JSON schema.\n"
                        f"Errors: {json.dumps(errors)}\n"
                        "Reply again with ONLY a JSON object that matches this schema:\n"
                        f"{json.dumps(schema.model_json_schema())}"
                    ),
                },
            ],
            json_mode=True,
            context={**request.context, "corrective": True, "errors": errors},
        )
        second = self.complete(correction, recorder=recorder, purpose="corrective_reprompt")
        usage = _add_usage(first.usage, second.usage)
        latency = first.latency_ms + second.latency_ms
        try:
            value = schema.model_validate(extract_json(second.content))
        except (ValueError, ValidationError) as exc:
            raise LLMSchemaError(
                f"{request.node}: output failed {schema.__name__} validation after a corrective "
                "re-prompt",
                _schema_errors(exc),
                second.content,
            ) from exc
        return StructuredResult(value, usage, latency, reprompted=True)


# ---------------------------------------------------------------------- real provider


class LiteLLMBackend:
    def __init__(self, settings: Settings, model: str) -> None:
        self.provider = settings.llm_provider
        self.model = litellm_model(self.provider, model)
        self.temperature = settings.llm_temperature
        self.api_base = settings.ollama_api_base if self.provider == "ollama" else None
        key = {"groq": settings.groq_api_key, "gemini": settings.gemini_api_key}.get(self.provider)
        self._api_key = key.get_secret_value() if key else None

    def complete(self, request: LLMRequest, *, timeout: float) -> Completion:
        import litellm  # slow import; only needed for real providers

        litellm.suppress_debug_info = True
        litellm.telemetry = False
        logging.getLogger("LiteLLM").setLevel(logging.WARNING)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": request.messages,
            "temperature": self.temperature,
            "timeout": timeout,
            "num_retries": 0,  # retries are ours, so they are throttled and audited
        }
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if request.tools:
            kwargs["tools"] = request.tools
            kwargs["tool_choice"] = "auto"
        if request.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        resp = litellm.completion(**kwargs)
        message = resp.choices[0].message
        calls = [
            ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments or "{}")
            for c in (getattr(message, "tool_calls", None) or [])
        ]
        usage = getattr(resp, "usage", None)
        return Completion(
            content=message.content,
            tool_calls=calls,
            usage=TokenUsage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ),
        )


# ---------------------------------------------------------------------- factory


@lru_cache
def _shared_throttle(provider: str, rpm: int, tpm: int) -> Throttle:
    """One throttle per process and provider, shared by concurrent runs."""
    return Throttle(rpm, tpm)


def build_client(settings: Settings | None = None, *, backend: Backend | None = None) -> LLMClient:
    settings = settings or get_settings()
    provider = settings.llm_provider
    model = resolve_model(provider, settings.llm_model)
    if backend is None:
        if provider == "fake":
            from payops.llm.fake import FakeLLM

            backend = FakeLLM()
        else:
            backend = LiteLLMBackend(settings, model)
    throttle = (
        None
        if provider == "fake"
        else _shared_throttle(
            provider, settings.llm_requests_per_minute, settings.llm_tokens_per_minute
        )
    )
    return LLMClient(
        backend,
        provider=provider,
        model=model,
        throttle=throttle,
        retry_policy=RetryPolicy(
            max_retries=settings.llm_max_retries,
            base_seconds=settings.llm_backoff_base_seconds,
            max_seconds=settings.llm_backoff_max_seconds,
        ),
        timeout_seconds=settings.llm_timeout_seconds,
    )
