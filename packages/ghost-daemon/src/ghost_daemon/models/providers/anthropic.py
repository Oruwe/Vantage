from __future__ import annotations

import time

import anthropic as _sdk

from ghost_daemon.metrics import (
    embedder_duration_seconds, llm_call_duration_seconds, llm_calls_total,
    llm_tokens_total,
)
from ghost_daemon.models.providers._keychain import resolve_api_key


class AnthropicProvider:
    def __init__(self, model: str, api_key_env: str = "ANTHROPIC_API_KEY") -> None:
        api_key = resolve_api_key(api_key_env)
        self._client = _sdk.AsyncAnthropic(api_key=api_key)
        self._model = model

    @property
    def model_id(self) -> str:
        return f"anthropic/{self._model}"

    async def complete(self, messages: list[dict], *, max_tokens: int = 600) -> str:
        provider, model, operation = "anthropic", self._model, "messages.create"
        t0 = time.monotonic()
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
            )
            usage = getattr(response, "usage", None)
            if usage is not None:
                in_tok = int(getattr(usage, "input_tokens", 0) or 0)
                out_tok = int(getattr(usage, "output_tokens", 0) or 0)
                if in_tok:
                    llm_tokens_total.labels(provider=provider, model=model, kind="input").inc(in_tok)
                if out_tok:
                    llm_tokens_total.labels(provider=provider, model=model, kind="output").inc(out_tok)
            llm_calls_total.labels(
                provider=provider, model=model, operation=operation, outcome="ok",
            ).inc()
            return response.content[0].text  # type: ignore[union-attr]
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
        # Anthropic doesn't expose a first-party embedding API yet; return a
        # zero vector so Qdrant upserts still work — replace with Voyage in M2.
        embedder_duration_seconds.labels(backend="anthropic", outcome="noop").observe(0.0)
        return [0.0] * 1536
