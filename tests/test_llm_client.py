import json

import pytest

from knowledge_runtime.errors import KRProviderUnavailable
from knowledge_runtime.llm_client import OpenAICompatibleClient


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {"content": "ok", "tool_calls": None}}]}).encode()


def test_client_uses_compatible_chat_completions_and_configured_model(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data)
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(
        api_key="test-only-secret",
        base_url="https://example.test/compatible/v1",
        model="qwen3.8-max",
    )

    turn = client.complete([{"role": "user", "content": "hello"}], [])

    assert captured["url"] == "https://example.test/compatible/v1/chat/completions"
    assert captured["body"]["model"] == "qwen3.8-max"
    assert captured["body"]["tool_choice"] == "none"
    assert captured["headers"]["Authorization"] == "Bearer test-only-secret"
    assert turn.content == "ok"


def test_client_rejects_models_outside_user_allowlist():
    with pytest.raises(ValueError, match="deepseek-v4.1-flash"):
        OpenAICompatibleClient(api_key="test", model="unapproved-model")


def test_client_defaults_to_qwen_for_agent_runtime(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)

    client = OpenAICompatibleClient(api_key="test-only-secret")

    assert client.model == "qwen3.8-max"


def test_client_requires_key_without_echoing_it(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    client = OpenAICompatibleClient(api_key="", model="deepseek-v4.1-flash")

    with pytest.raises(KRProviderUnavailable, match="LLM_API_KEY"):
        client.complete([], [])
