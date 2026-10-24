from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_VECTOR_SIZE = 768


class Embedder:
    """Lightweight text embedder backed by Ollama (or any OpenAI-compat embed endpoint).

    Falls back to zero-vector gracefully so the store still works without it — just
    without semantic search capability.
    """

    def __init__(
        self,
        provider: str = "ollama",
        model: str = "nomic-embed-text",
        endpoint: str = "http://localhost:11434",
        api_key_env: str = "",
    ) -> None:
        self._provider = provider.lower()
        self._model = model
        self._endpoint = endpoint.strip("/")
        self._api_key_env = api_key_env
        self._available: bool | None = None  # None = not yet probed

    async def embed(self, text: str) -> list[float]:
        """Return a 768-dim vector for text. Returns zeros if backend unavailable."""
        if self._available is False:
            return [0.0] * _VECTOR_SIZE
        try:
            if self._provider == "ollama":
                return await self._ollama_embed(text)
            return await self._openai_embed(text)
        except Exception as exc:
            if self._available is None:
                logger.warning("Embedder unavailable (%s) — semantic search disabled", exc)
            self._available = False
            return [0.0] * _VECTOR_SIZE

    async def _ollama_embed(self, text: str) -> list[float]:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self._endpoint}/api/embed",
                json={"model": self._model, "input": text},
            )
            resp.raise_for_status()
            data = resp.json()

        # Ollama /api/embed returns {"embeddings": [[...]]} (batch API)
        embeddings = data.get("embeddings") or data.get("embedding")
        if isinstance(embeddings, list) and embeddings:
            vec = embeddings[0] if isinstance(embeddings[0], list) else embeddings
        else:
            raise ValueError(f"Unexpected Ollama embed response: {list(data.keys())}")

        self._available = True
        return _fit(vec)

    async def _openai_embed(self, text: str) -> list[float]:
        import os
        import httpx

        api_key = os.environ.get(self._api_key_env, "") if self._api_key_env else ""
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        endpoint = self._endpoint or "https://api.openai.com/v1"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{endpoint}/embeddings",
                headers=headers,
                json={"model": self._model, "input": text},
            )
            resp.raise_for_status()
            data = resp.json()

        vec = data["data"][0]["embedding"]
        self._available = True
        return _fit(vec)


def _fit(v: list[float]) -> list[float]:
    if len(v) == _VECTOR_SIZE:
        return v
    if len(v) < _VECTOR_SIZE:
        return v + [0.0] * (_VECTOR_SIZE - len(v))
    return v[:_VECTOR_SIZE]
