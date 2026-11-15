import json
import types
import uuid
from typing import Any

import pytest

from payops.agents.scoring import MAX_LLM_ADJUSTMENT, final_score
from payops.config import Settings
from payops.llm import client as client_mod
from payops.llm.client import (
    LiteLLMBackend,
    LLMCallRecord,
    LLMClient,
    LLMRequest,
    LLMSchemaError,
    build_client,
    extract_json,
)
from payops.llm.fake import FakeLLM, FakeToolCalls, choose_action
from payops.llm.models import DEFAULT_MODELS, litellm_model, resolve_model
from payops.llm.retry import LLMUnavailableError, RetryPolicy
from payops.schemas.llm import DraftOutput, TriageOutput


class RateLimitedError(Exception):
    status_code = 429


# Ignore the developer's .env so tests see only what they set.
NO_ENV: dict[str, Any] = {"_env_file": None, "database_url": "postgresql://x/y"}

GOOD_DRAFT = {
    "action_type": "reminder",
    "tone": "friendly",
    "subject": "Reminder",
    "body": "Please pay invoice INV-1.",
    "rationale": "Slightly late.",
}


def make_client(fake: FakeLLM, retries: int = 2) -> tuple[LLMClient, list[float]]:
    sleeps: list[float] = []
    client = LLMClient(
        fake,
        provider="fake",
        model="fake-v1",
        retry_policy=RetryPolicy(max_retries=retries, base_seconds=0.5, max_seconds=1),
        sleep=sleeps.append,
    )
    return client, sleeps


def req(node: str = "drafting", **context: Any) -> LLMRequest:
    return LLMRequest(node, [{"role": "user", "content": "hi"}], context=context)


def test_structured_valid_first_try() -> None:
    fake = FakeLLM({("drafting", "*"): [GOOD_DRAFT]})
    records: list[LLMCallRecord] = []
    client, _ = make_client(fake)
    result = client.structured(req(), DraftOutput, recorder=records.append)
    assert result.value.action_type == "reminder"
    assert not result.reprompted
    assert result.usage.total_tokens > 0
    assert [r.purpose for r in records] == ["call"]
    assert fake.requests[0].json_mode is True


def test_structured_corrective_reprompt_then_success() -> None:
    fake = FakeLLM({("drafting", "*"): ["not json at all", {**GOOD_DRAFT, "tone": "firm"}]})
    records: list[LLMCallRecord] = []
    client, _ = make_client(fake)
    result = client.structured(req(), DraftOutput, recorder=records.append)
    assert result.reprompted
    assert result.value.tone == "firm"
    assert [r.purpose for r in records] == ["call", "corrective_reprompt"]
    correction = fake.requests[1]
    assert "did not match the required JSON schema" in correction.messages[-1]["content"]
    assert correction.messages[-2] == {"role": "assistant", "content": "not json at all"}
    assert result.usage.total_tokens == sum(r.usage.total_tokens for r in records if r.usage)


def test_structured_fails_cleanly_after_one_reprompt() -> None:
    bad = {**GOOD_DRAFT, "action_type": "send_lawyers"}
    fake = FakeLLM({("drafting", "*"): [bad]})
    client, _ = make_client(fake)
    with pytest.raises(LLMSchemaError) as info:
        client.structured(req(), DraftOutput)
    assert len(fake.requests) == 2  # exactly one corrective re-prompt
    assert info.value.errors[0]["loc"] == ["action_type"]


def test_complete_retries_429_then_succeeds() -> None:
    fake = FakeLLM({("drafting", "*"): [RateLimitedError("slow down"), GOOD_DRAFT]})
    records: list[LLMCallRecord] = []
    client, sleeps = make_client(fake)
    resp = client.complete(req(), recorder=records.append)
    assert json.loads(resp.content or "") == GOOD_DRAFT
    assert resp.attempts == 2
    assert len(sleeps) == 1
    assert len(records) == 1
    assert records[0].error is None


def test_complete_429_exhausted_is_recorded_and_raised() -> None:
    fake = FakeLLM({("drafting", "*"): [RateLimitedError("slow down")]})
    records: list[LLMCallRecord] = []
    client, sleeps = make_client(fake, retries=2)
    with pytest.raises(LLMUnavailableError):
        client.complete(req(), recorder=records.append)
    assert len(fake.requests) == 3
    assert len(sleeps) == 2
    assert records[0].response is None
    assert "3 attempts" in (records[0].error or "")


def test_scripted_by_invoice_number_beats_wildcard() -> None:
    fake = FakeLLM()
    fake.add("drafting", "*", {**GOOD_DRAFT, "subject": "wild"})
    fake.add("drafting", "INV-7", {**GOOD_DRAFT, "subject": "specific"})
    client, _ = make_client(fake)
    assert client.structured(req(invoice_number="INV-7"), DraftOutput).value.subject == "specific"
    assert client.structured(req(invoice_number="INV-8"), DraftOutput).value.subject == "wild"


def test_fake_tool_calls_and_callables() -> None:
    fake = FakeLLM()
    fake.add("research", "*", lambda r: FakeToolCalls([("get_customer", {"customer_id": "x"})]))
    client, _ = make_client(fake)
    resp = client.complete(req("research"))
    assert resp.content is None
    assert resp.tool_calls[0].name == "get_customer"
    assert resp.tool_calls[0].parsed_arguments() == {"customer_id": "x"}


def test_fake_heuristic_research_walks_read_tools() -> None:
    fake = FakeLLM()
    client, _ = make_client(fake)
    messages: list[dict] = []
    names = []
    for _ in range(4):
        resp = client.complete(LLMRequest("research", list(messages), context={"customer_id": "c"}))
        if not resp.tool_calls:
            break
        call = resp.tool_calls[0]
        names.append(call.name)
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [{"function": {"name": call.name, "arguments": call.arguments}}],
            }
        )
    assert names == ["get_customer", "get_invoices", "get_payment_history"]
    assert resp.content
    assert "Research complete" in resp.content


def test_fake_heuristic_triage_echoes_base_scores() -> None:
    invoice_id = str(uuid.uuid4())
    fake = FakeLLM()
    client, _ = make_client(fake)
    context = {"invoices": [{"invoice_id": invoice_id, "base_score": 42, "days_overdue": 3,
                             "balance": "10.00"}]}  # fmt: skip
    out = client.structured(req("triage", **context), TriageOutput).value
    assert out.priorities[0].priority_score == 42


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        ({"status": "disputed", "balance": "100", "days_overdue": 20}, "escalate_to_human"),
        ({"status": "paid", "balance": "0"}, "no_action"),
        ({"status": "overdue", "balance": "100", "active_plan": True}, "no_action"),
        ({"status": "overdue", "balance": "100", "partial_payment_count": 2}, "payment_plan_offer"),
        ({"status": "overdue", "balance": "100", "broken_promises": 1}, "payment_plan_offer"),
        ({"status": "overdue", "balance": "100", "days_overdue": 5}, "reminder"),
        ({"status": "open", "balance": "100", "days_overdue": 0}, "no_action"),
    ],
)
def test_fake_choose_action(facts: dict, expected: str) -> None:
    assert choose_action(facts)[0] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Sure! Here it is: {"a": {"b": 2}} hope that helps', {"a": {"b": 2}}),
    ],
)
def test_extract_json(text: str, expected: dict) -> None:
    assert extract_json(text) == expected


@pytest.mark.parametrize("text", [None, "", "no braces here", "} backwards {"])
def test_extract_json_errors(text: str | None) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        extract_json(text)


def test_models_resolution() -> None:
    assert resolve_model("groq", None) == DEFAULT_MODELS["groq"]
    assert resolve_model("groq", "custom") == "custom"
    assert resolve_model("fake", "openai/gpt-oss-120b") == "fake-v1"  # leftover real model
    assert resolve_model("fake", "fake-v2") == "fake-v2"
    assert litellm_model("groq", "openai/gpt-oss-120b") == "groq/openai/gpt-oss-120b"
    assert litellm_model("ollama", "llama3.2") == "ollama_chat/llama3.2"
    assert litellm_model("gemini", "gemini/x") == "gemini/x"


def test_build_client_fake_has_no_throttle() -> None:
    client = build_client(Settings(llm_provider="fake", **NO_ENV))
    assert isinstance(client.backend, FakeLLM)
    assert client.throttle is None
    assert client.model == "fake-v1"


def test_blank_llm_model_means_default() -> None:
    settings = Settings(llm_provider="groq", llm_model=" ", **NO_ENV)
    assert settings.llm_model is None


def test_litellm_backend_call_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real backend passes our timeout, disables LiteLLM's own retries, and maps output."""
    seen: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> Any:
        seen.update(kwargs)
        call = types.SimpleNamespace(
            id="c1", function=types.SimpleNamespace(name="get_customer", arguments='{"a":1}')
        )
        message = types.SimpleNamespace(content="hello", tool_calls=[call])
        usage = types.SimpleNamespace(prompt_tokens=11, completion_tokens=5)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=usage)

    import litellm

    monkeypatch.setattr(litellm, "completion", fake_completion)
    settings = Settings(
        llm_provider="groq",
        groq_api_key="gsk-test-not-real",  # type: ignore[arg-type]
        **NO_ENV,
    )
    client = build_client(settings)
    assert isinstance(client.backend, LiteLLMBackend)
    assert client.throttle is not None
    request = LLMRequest("research", [{"role": "user", "content": "x"}], tools=[{"t": 1}])
    request.json_mode = True
    out = client.backend.complete(request, timeout=12)
    assert seen["model"] == "groq/openai/gpt-oss-120b"
    assert seen["timeout"] == 12
    assert seen["num_retries"] == 0
    assert seen["api_key"] == "gsk-test-not-real"
    assert seen["tools"] == [{"t": 1}]
    assert seen["response_format"] == {"type": "json_object"}
    assert out.content == "hello"
    assert out.tool_calls[0].name == "get_customer"
    assert out.usage.total_tokens == 16


def test_ollama_backend_uses_api_base() -> None:
    settings = Settings(llm_provider="ollama", ollama_api_base="http://h:1", **NO_ENV)
    backend = LiteLLMBackend(settings, "llama3.2")
    assert backend.api_base == "http://h:1"
    assert backend.model == "ollama_chat/llama3.2"


def test_shared_throttle_is_per_process() -> None:
    a = client_mod._shared_throttle("groq", 10, 100)
    assert a is client_mod._shared_throttle("groq", 10, 100)


@pytest.mark.parametrize(
    ("base", "llm", "expected"),
    [
        (50, None, 50),
        (50, 60, 60),
        (50, 100, 50 + MAX_LLM_ADJUSTMENT),
        (50, 0, 50 - MAX_LLM_ADJUSTMENT),
        (95, 100, 100),
        (5, 0, 0),
    ],
)
def test_triage_score_is_clamped(base: int, llm: int | None, expected: int) -> None:
    assert final_score(base, llm) == expected
