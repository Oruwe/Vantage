from __future__ import annotations

import time

import httpx

from ghost_daemon.metrics import (
    embedder_duration_seconds, llm_call_duration_seconds, llm_calls_total,
    llm_tokens_total,
)


class OllamaProvider:
    def __init__(
        self,
        model: str,
        endpoint: str = "http://localhost:11434",
        embed_model: str = "nomic-embed-text",
    ) -> None:
        self._model = model
        self._embed_model = embed_model
        self._endpoint = endpoint.rstrip("/")

    @property
    def model_id(self) -> str:
        return f"ollama/{self._model}"

    async def complete(self, messages: list[dict], *, max_tokens: int = 600) -> str:
        provider, model, operation = "ollama", self._model, "chat"
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(
                    f"{self._endpoint}/api/chat",
                    json={"model": model, "messages": messages, "stream": False},
                )
                r.raise_for_status()
                data = r.json()
            # Ollama reports token counts under *_count fields.
            in_tok = int(data.get("prompt_eval_count", 0) or 0)
            out_tok = int(data.get("eval_count", 0) or 0)
            if in_tok:
                llm_tokens_total.labels(provider=provider, model=model, kind="input").inc(in_tok)
            if out_tok:
                llm_tokens_total.labels(provider=provider, model=model, kind="output").inc(out_tok)
            llm_calls_total.labels(
                provider=provider, model=model, operation=operation, outcome="ok",
            ).inc()
            return data["message"]["content"]
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
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    f"{self._endpoint}/api/embeddings",
                    json={"model": self._embed_model, "prompt": text},
                )
                r.raise_for_status()
                vec = r.json()["embedding"]
            embedder_duration_seconds.labels(backend="ollama", outcome="ok").observe(
                time.monotonic() - t0,
            )
            return vec
        except Exception:
            embedder_duration_seconds.labels(backend="ollama", outcome="error").observe(
                time.monotonic() - t0,
            )
            raise
