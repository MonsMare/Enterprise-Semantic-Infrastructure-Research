from __future__ import annotations

import pytest

from knowledge_runtime.v2.config import RuntimeConfig
from knowledge_runtime.v2.model_gateway import LocalModelGateway


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict], list[dict]]] = []

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        self.calls.append((messages, tools))
        return {"ok": True}


def test_local_gateway_delegates_without_cloud_fallback() -> None:
    client = FakeClient()
    config = RuntimeConfig.test_private(
        local_model_base_url="http://127.0.0.1:8000/v1",
        local_model_name="private-model",
    )
    gateway = LocalModelGateway(config=config, client=client)

    result = gateway.complete([{"role": "user", "content": "hello"}], [])

    assert result == {"ok": True}
    assert client.calls == [([{"role": "user", "content": "hello"}], [])]
    assert gateway.model == "private-model"


def test_local_gateway_requires_explicit_local_endpoint() -> None:
    with pytest.raises(ValueError, match="KR_LOCAL_MODEL_BASE_URL"):
        LocalModelGateway(config=RuntimeConfig.test_private(), client=FakeClient())

