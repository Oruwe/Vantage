from __future__ import annotations

import httpx


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
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"{self._endpoint}/api/chat",
                json={"model": self._model, "messages": messages, "stream": False},
            )
            r.raise_for_status()
            return r.json()["message"]["content"]

    async def embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{self._endpoint}/api/embeddings",
                json={"model": self._embed_model, "prompt": text},
            )
            r.raise_for_status()
            return r.json()["embedding"]
