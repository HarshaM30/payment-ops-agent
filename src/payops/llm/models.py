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


def resolve_model(provider: str, model: str | None) -> str:
    return model or DEFAULT_MODELS[provider]


def litellm_model(provider: str, model: str) -> str:
    prefix = _LITELLM_PREFIX[provider]
    return model if model.startswith(f"{prefix}/") else f"{prefix}/{model}"
