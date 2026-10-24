from __future__ import annotations

import os

from openai import AsyncOpenAI
from ghost_daemon.models.providers._keychain import resolve_api_key

# Pre-configured endpoints for known providers
_ENDPOINTS: dict[str, tuple[str, str]] = {
    "grok":       ("https://api.x.ai/v1",              "XAI_API_KEY"),
    "groq":       ("https://api.groq.com/openai/v1",   "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1",     "OPENROUTER_API_KEY"),
}


class OpenAICompatProvider:
    """Works with any OpenAI-wire-compatible endpoint: xAI Grok, OpenRouter,
    vLLM, LM Studio, LiteLLM proxy, Azure OpenAI, etc."""

    def __init__(
        self,
        model: str,
        endpoint: str,
        api_key_env: str,
    ) -> None:
        api_key = resolve_api_key(api_key_env)
        self._model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=endpoint)

    @property
    def model_id(self) -> str:
        return f"openai_compat/{self._model}"

    async def complete(self, messages: list[dict], *, max_tokens: int = 600) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,  # type: ignore[arg-type]
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""

    async def embed(self, text: str) -> list[float]:
        # xAI / most OpenAI-compat providers do not expose embeddings yet.
        # Return a zero vector so Qdrant upserts remain functional; swap for
        # a real embeddings call once the provider supports it.
        return [0.0] * 1536


def build_grok(model: str, api_key_env: str = "XAI_API_KEY") -> OpenAICompatProvider:
    endpoint, default_env = _ENDPOINTS["grok"]
    return OpenAICompatProvider(
        model=model,
        endpoint=endpoint,
        api_key_env=api_key_env or default_env,
    )


def build_groq(model: str, api_key_env: str = "GROQ_API_KEY") -> OpenAICompatProvider:
    endpoint, default_env = _ENDPOINTS["groq"]
    return OpenAICompatProvider(
        model=model,
        endpoint=endpoint,
        api_key_env=api_key_env or default_env,
    )


def build_openrouter(model: str, api_key_env: str = "OPENROUTER_API_KEY") -> OpenAICompatProvider:
    endpoint, default_env = _ENDPOINTS["openrouter"]
    return OpenAICompatProvider(
        model=model,
        endpoint=endpoint,
        api_key_env=api_key_env or default_env,
    )
