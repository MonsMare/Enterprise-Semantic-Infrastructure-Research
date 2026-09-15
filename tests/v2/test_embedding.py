from __future__ import annotations

import pytest

from knowledge_runtime.v2.config import RuntimeConfig
from knowledge_runtime.v2.embedding import DashScopeEmbeddingGateway


class RecordingTransport:
    def __init__(self) -> None:
        self.headers = {}
        self.body = {}
        self.calls = 0

    def request(self, method, url, *, headers=None, json_body=None):
        self.calls += 1
        self.headers = headers or {}
        self.body = json_body or {}
        return {"status": 200, "json": {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}}


def test_embedding_gateway_uses_dashscope_key_only() -> None:
    transport = RecordingTransport()
    config = RuntimeConfig.test_private(
        allow_remote_embedding=True,
        embedding_api_key="embedding-secret",
        embedding_base_url="https://dashscope.example/v1",
    )
    gateway = DashScopeEmbeddingGateway(config=config, transport=transport)

    vectors = gateway.embed_batch(["claim"])

    assert transport.headers["Authorization"] == "Bearer embedding-secret"
    assert transport.body["model"] == "qwen3.7-text-embedding"
    assert vectors == [[0.1, 0.2]]


def test_embedding_gateway_caches_same_batch() -> None:
    transport = RecordingTransport()
    config = RuntimeConfig.test_private(
        allow_remote_embedding=True,
        embedding_api_key="embedding-secret",
        embedding_base_url="https://dashscope.example/v1",
    )
    gateway = DashScopeEmbeddingGateway(config=config, transport=transport)
    gateway.embed_batch(["claim"])
    gateway.embed_batch(["claim"])
    assert transport.calls == 1


def test_embedding_gateway_fails_closed_in_private_mode() -> None:
    with pytest.raises(RuntimeError, match="remote embedding"):
        DashScopeEmbeddingGateway(config=RuntimeConfig.test_private())

