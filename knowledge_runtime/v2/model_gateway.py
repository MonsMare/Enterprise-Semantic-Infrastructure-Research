from __future__ import annotations

from typing import Any, Protocol, Sequence

from .config import RuntimeConfig


class ModelGateway(Protocol):
    model: str

    def complete(self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]) -> Any:
        ...


class LocalModelGateway:
    """Small dependency-injected seam for a locally hosted OpenAI-compatible model."""

    def __init__(self, *, config: RuntimeConfig, client: Any) -> None:
        if not config.local_model_base_url:
            raise ValueError("KR_LOCAL_MODEL_BASE_URL is required for LocalModelGateway")
        self.model = config.local_model_name or "local-model"
        self._client = client

    def complete(self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]) -> Any:
        return self._client.complete(list(messages), list(tools))

