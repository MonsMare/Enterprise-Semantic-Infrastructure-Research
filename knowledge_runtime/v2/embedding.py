from __future__ import annotations

import hashlib
import os
from typing import Any, Protocol, Sequence

from .config import RuntimeConfig


class EmbeddingTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...


class UrllibEmbeddingTransport:
    def request(self, method: str, url: str, *, headers=None, json_body=None) -> dict[str, Any]:
        import json
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            url,
            data=json.dumps(json_body or {}).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read()
                return {"status": response.status, "json": json.loads(raw.decode("utf-8"))}
        except urllib.error.HTTPError as exc:
            return {"status": exc.code, "content": exc.read()}


class EmbeddingGateway(Protocol):
    model: str

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]: ...


class DashScopeEmbeddingGateway:
    model = "qwen3.7-text-embedding"

    def __init__(self, *, config: RuntimeConfig, transport: EmbeddingTransport | None = None) -> None:
        config.require_remote_embedding()
        if not config.embedding_api_key or not config.embedding_base_url:
            raise ValueError("DASHSCOPE_API_KEY and DASHSCOPE_BASE_URL are required")
        self.config = config
        self.transport = transport or UrllibEmbeddingTransport()
        self.endpoint = config.embedding_base_url.rstrip("/") + "/embeddings"
        self._cache: dict[tuple[str, str, str], list[float]] = {}

    @classmethod
    def from_env(cls, *, transport: EmbeddingTransport | None = None) -> "DashScopeEmbeddingGateway":
        return cls(config=RuntimeConfig.from_env(), transport=transport)

    def _cache_key(self, text: str) -> tuple[str, str, str]:
        return (hashlib.sha256(text.encode("utf-8")).hexdigest(), self.model, self.endpoint)

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        values = [str(text) for text in texts]
        if not values:
            return []
        result: list[list[float] | None] = [None] * len(values)
        missing: list[tuple[int, str]] = []
        for index, text in enumerate(values):
            cached = self._cache.get(self._cache_key(text))
            if cached is None:
                missing.append((index, text))
            else:
                result[index] = list(cached)
        if missing:
            response = self.transport.request(
                "POST",
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.config.embedding_api_key}",
                    "Content-Type": "application/json",
                },
                json_body={"model": self.model, "input": [text for _, text in missing]},
            )
            if not 200 <= int(response.get("status", 500)) < 300:
                raise RuntimeError(f"embedding request failed with HTTP {response.get('status')}")
            payload = response.get("json") or {}
            rows = payload.get("data") or []
            if len(rows) != len(missing):
                raise RuntimeError("embedding response count does not match request")
            for (index, text), row in zip(missing, rows):
                vector = [float(value) for value in row.get("embedding", [])]
                if not vector:
                    raise RuntimeError("embedding response contains an empty vector")
                self._cache[self._cache_key(text)] = vector
                result[index] = list(vector)
        return [vector or [] for vector in result]

