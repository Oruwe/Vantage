from __future__ import annotations

from typing import Protocol, runtime_checkable

from ghost_daemon.config import Config


@runtime_checkable
class LLMProvider(Protocol):
    @property
    def model_id(self) -> str: ...

    async def complete(self, messages: list[dict], *, max_tokens: int = 600) -> str: ...

    async def embed(self, text: str) -> list[float]: ...


def build_from_config(cfg: Config) -> LLMProvider:
    provider = cfg.llm.provider.lower()

    if provider == "anthropic":
        from ghost_daemon.models.providers.anthropic import AnthropicProvider
        return AnthropicProvider(model=cfg.llm.model, api_key_env=cfg.llm.api_key_env)

    if provider == "ollama":
        from ghost_daemon.models.providers.ollama import OllamaProvider
        return OllamaProvider(
            model=cfg.llm.model,
            endpoint=cfg.llm.endpoint or "http://localhost:11434",
            embed_model=cfg.embeddings.model,
        )

    if provider == "grok":
        from ghost_daemon.models.providers.openai_compat import build_grok
        return build_grok(model=cfg.llm.model, api_key_env=cfg.llm.api_key_env or "XAI_API_KEY")

    if provider == "groq":
        from ghost_daemon.models.providers.openai_compat import build_groq
        return build_groq(model=cfg.llm.model, api_key_env=cfg.llm.api_key_env or "GROQ_API_KEY")

    if provider in ("openrouter", "openai_compat"):
        from ghost_daemon.models.providers.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(
            model=cfg.llm.model,
            endpoint=cfg.llm.endpoint,
            api_key_env=cfg.llm.api_key_env,
        )

    raise ValueError(
        f"Unknown LLM provider {provider!r}. "
        "Supported values: 'grok', 'anthropic', 'ollama', 'openrouter', 'openai_compat'."
    )
