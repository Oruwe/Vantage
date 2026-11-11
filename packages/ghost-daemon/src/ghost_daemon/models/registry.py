from __future__ import annotations

from typing import Protocol, runtime_checkable

from ghost_daemon.config import Config


@runtime_checkable
class LLMProvider(Protocol):
    @property
    def model_id(self) -> str: ...

    async def complete(self, messages: list[dict], *, max_tokens: int = 600) -> str: ...

    async def embed(self, text: str) -> list[float]: ...


class _NullLLMProvider:
    """Stand-in provider used when no real LLM is configured.

    Lets the daemon boot without an API key — useful for tests and for the
    coming offline demo mode. Completion raises so a real call path blows up
    loud instead of silently returning an empty draft. Embedding returns a
    zero vector so downstream code that only wants a shape keeps working.
    """
    model_id = "null"

    async def complete(self, messages: list[dict], *, max_tokens: int = 600) -> str:
        raise RuntimeError(
            "No LLM provider is configured (cfg.llm.provider is empty). "
            "Set it to 'groq', 'anthropic', 'ollama', 'grok', 'openrouter', "
            "or 'openai_compat' in config.toml, or wire a real provider in."
        )

    async def embed(self, text: str) -> list[float]:
        return [0.0] * 768


def build_from_config(cfg: Config) -> LLMProvider:
    provider = cfg.llm.provider.lower()

    if not provider:
        return _NullLLMProvider()

    if provider == "mock":
        from ghost_daemon.models.providers.mock import MockLLMProvider
        return MockLLMProvider()

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
