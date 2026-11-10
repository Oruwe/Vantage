from __future__ import annotations

import time

from openai import AsyncOpenAI

from ghost_daemon.metrics import (
    embedder_duration_seconds, llm_call_duration_seconds, llm_calls_total,
    llm_tokens_total,
)
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
        provider_name: str = "openai_compat",
    ) -> None:
        api_key = resolve_api_key(api_key_env)
        self._model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=endpoint)
        self._provider = provider_name

    @property
    def model_id(self) -> str:
        return f"openai_compat/{self._model}"

    async def complete(self, messages: list[dict], *, max_tokens: int = 600) -> str:
        provider, model, operation = self._provider, self._model, "chat.completions.create"
        t0 = time.monotonic()
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                max_tokens=max_tokens,
            )
            usage = getattr(response, "usage", None)
            if usage is not None:
                in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
                out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
                if in_tok:
                    llm_tokens_total.labels(provider=provider, model=model, kind="input").inc(in_tok)
                if out_tok:
                    llm_tokens_total.labels(provider=provider, model=model, kind="output").inc(out_tok)
            llm_calls_total.labels(
                provider=provider, model=model, operation=operation, outcome="ok",
            ).inc()
            return response.choices[0].message.content or ""
        except Exception:
            llm_calls_total.labels(
                provider=provider, model=model, operation=operation, outcome="error",
            ).inc()
            raise
        finally:
            llm_call_duration_seconds.labels(
                provider=provider, model=model, operation=operation,
            ).observe(time.monotonic() - t0)

    async def embed(self, text: str) -> list[float]:
        # xAI / most OpenAI-compat providers do not expose embeddings yet.
        # Return a zero vector so Qdrant upserts remain functional; swap for
        # a real embeddings call once the provider supports it.
        embedder_duration_seconds.labels(backend=self._provider, outcome="noop").observe(0.0)
        return [0.0] * 1536


def build_grok(model: str, api_key_env: str = "XAI_API_KEY") -> OpenAICompatProvider:
    endpoint, default_env = _ENDPOINTS["grok"]
    return OpenAICompatProvider(
        model=model,
        endpoint=endpoint,
        api_key_env=api_key_env or default_env,
        provider_name="grok",
    )


def build_groq(model: str, api_key_env: str = "GROQ_API_KEY") -> OpenAICompatProvider:
    endpoint, default_env = _ENDPOINTS["groq"]
    return OpenAICompatProvider(
        model=model,
        endpoint=endpoint,
        api_key_env=api_key_env or default_env,
        provider_name="groq",
    )


def build_openrouter(model: str, api_key_env: str = "OPENROUTER_API_KEY") -> OpenAICompatProvider:
    endpoint, default_env = _ENDPOINTS["openrouter"]
    return OpenAICompatProvider(
        model=model,
        endpoint=endpoint,
        api_key_env=api_key_env or default_env,
        provider_name="openrouter",
    )
