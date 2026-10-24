from __future__ import annotations

import anthropic as _sdk
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
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=messages,
        )
        return response.content[0].text  # type: ignore[union-attr]

    async def embed(self, text: str) -> list[float]:
        # Anthropic doesn't expose a first-party embedding API yet; return a
        # zero vector so Qdrant upserts still work — replace with Voyage in M2.
        return [0.0] * 1536
