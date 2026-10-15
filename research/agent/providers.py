"""Provider registry supporting multiple OpenAI-compatible LLM endpoints."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol
from urllib.request import Request, urlopen

from research.utils import ResearchConfig


class ProviderError(RuntimeError):
    """Raised when a configured LLM provider cannot produce a response."""


class LlmProvider(Protocol):
    name: str

    async def complete(
        self, messages: list[dict[str, str]], actions: dict[str, str]
    ) -> str: ...


class FakeProvider:
    """Deterministic provider for tests and offline framework validation."""

    name = "fake"

    def __init__(self, action: str = 'submit("No")') -> None:
        self.action = action

    async def complete(self, messages: list[dict[str, str]], actions: dict[str, str]) -> str:
        return f"```\n{self.action}\n```"


class OpenAICompatibleProvider:
    """Minimal standard-library client for OpenAI-compatible chat endpoints."""

    name = "openai-compatible"

    def __init__(self, config: ResearchConfig) -> None:
        if not config.aiops_llm_base_url or not config.aiops_llm_model:
            raise ProviderError("AIOPS_LLM_BASE_URL and AIOPS_LLM_MODEL are required")
        self.base_url = config.aiops_llm_base_url.rstrip("/")
        self.api_key = config.aiops_llm_api_key
        self.model = config.aiops_llm_model
        self.timeout = config.aiops_llm_timeout_seconds

    async def complete(self, messages: list[dict[str, str]], actions: dict[str, str]) -> str:
        return await asyncio.to_thread(self._complete_sync, messages, actions)

    def _complete_sync(self, messages: list[dict[str, str]], actions: dict[str, str]) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
        }
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:  # nosec B310
                body: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        except (OSError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError(f"LLM provider request failed: {error}") from error
        try:
            return str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderError("LLM response did not contain choices[0].message.content") from error


def build_provider(config: ResearchConfig) -> LlmProvider:
    provider = config.aiops_llm_provider.lower()
    if provider == "fake":
        return FakeProvider()
    if provider in {"openai", "azure", "qwen", "deepseek", "vllm", "openrouter", "groq", "openai-compatible"}:
        return OpenAICompatibleProvider(config)
    raise ProviderError(f"Unsupported AIOPS_LLM_PROVIDER: {config.aiops_llm_provider}")
