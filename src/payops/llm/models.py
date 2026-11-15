"""Provider -> default model names, and how each maps to a LiteLLM model string.

Defaults were checked against the providers' model and pricing pages on 2026-11-15:

- groq:   ``openai/gpt-oss-120b``      production model with tool calling and JSON mode,
          on the free plan (30 RPM, 8K TPM, 1K RPD at the time of writing). The Llama
          production models are marked Enterprise and return model_not_found on a free
          key. ``openai/gpt-oss-20b`` is a faster alternative with the same limits.
- gemini: ``gemini-3.5-flash-lite``    stable, "Free of charge" on the free tier.
- ollama: ``llama3.2``                 3B, runs locally on an Apple Silicon laptop, tools.
- fake:   deterministic scripted model used by tests, CI, load tests, eval smoke runs.

Override with ``LLM_MODEL``. Model names live only here and in ``.env.example``.
"""

from typing import Literal

Provider = Literal["groq", "gemini", "ollama", "fake"]

DEFAULT_MODELS: dict[str, str] = {
    "groq": "openai/gpt-oss-120b",
    "gemini": "gemini-3.5-flash-lite",
    "ollama": "llama3.2",
    "fake": "fake-v1",
}

# LiteLLM routes on a "<provider>/<model>" prefix. ollama_chat uses Ollama's chat API,
# which supports tool calling.
_LITELLM_PREFIX: dict[str, str] = {"groq": "groq", "gemini": "gemini", "ollama": "ollama_chat"}


# Free-tier daily quotas, used by the eval runner to estimate how many days a full eval
# needs. Groq values are from console.groq.com/docs/rate-limits (checked 2026-11-15):
# gpt-oss-120b and gpt-oss-20b both allow 30 RPM, 1K RPD, 8K TPM, 200K TPD.
# None means "no daily cap we plan around" (local Ollama, the fake provider).
DailyLimit = tuple[int | None, int | None]  # (tokens per day, requests per day)

FREE_TIER_DAILY_LIMITS: dict[tuple[str, str], DailyLimit] = {
    ("groq", "openai/gpt-oss-120b"): (200_000, 1_000),
    ("groq", "openai/gpt-oss-20b"): (200_000, 1_000),
}


def daily_limit(provider: str, model: str) -> DailyLimit:
    return FREE_TIER_DAILY_LIMITS.get((provider, model), (None, None))


def resolve_model(provider: str, model: str | None) -> str:
    # The fake provider ignores a real model name left in LLM_MODEL (e.g. switching to
    # LLM_PROVIDER=fake for a smoke run), so reports never mislabel fake results.
    if provider == "fake" and model and not model.startswith("fake"):
        return DEFAULT_MODELS["fake"]
    return model or DEFAULT_MODELS[provider]


def litellm_model(provider: str, model: str) -> str:
    prefix = _LITELLM_PREFIX[provider]
    return model if model.startswith(f"{prefix}/") else f"{prefix}/{model}"
