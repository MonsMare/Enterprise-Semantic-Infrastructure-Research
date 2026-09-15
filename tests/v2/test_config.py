from __future__ import annotations

import pytest

from knowledge_runtime.v2.config import RuntimeConfig


def test_private_config_disables_all_remote_gateways_by_default(monkeypatch):
    monkeypatch.delenv("QWEN_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    config = RuntimeConfig.from_env()

    assert config.private_mode is True
    assert config.allow_remote_parser is False
    assert config.allow_remote_embedding is False
    assert config.allow_remote_agent is False
    assert config.agent_model == "qwen3.8-max"
    assert config.embedding_model == "qwen3.7-text-embedding"


def test_test_private_never_reads_process_secrets(monkeypatch):
    monkeypatch.setenv("QWEN_LLM_API_KEY", "agent-secret")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "embedding-secret")

    config = RuntimeConfig.test_private()

    assert config.agent_api_key == ""
    assert config.embedding_api_key == ""
    assert config.allow_remote_agent is False


def test_remote_gateway_requires_explicit_flag(monkeypatch):
    monkeypatch.setenv("KR_ALLOW_REMOTE_AGENT", "true")
    monkeypatch.setenv("QWEN_LLM_API_KEY", "agent-secret")
    monkeypatch.setenv("QWEN_LLM_BASE_URL", "https://example.invalid/v1")

    config = RuntimeConfig.from_env()

    assert config.allow_remote_agent is True
    assert config.agent_api_key == "agent-secret"


def test_enabled_remote_agent_requires_key_and_endpoint(monkeypatch):
    monkeypatch.setenv("KR_ALLOW_REMOTE_AGENT", "true")
    monkeypatch.delenv("QWEN_LLM_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_LLM_BASE_URL", raising=False)

    with pytest.raises(ValueError, match="remote Agent"):
        RuntimeConfig.from_env()


def test_enabled_remote_embedding_requires_key_and_endpoint(monkeypatch):
    monkeypatch.setenv("KR_ALLOW_REMOTE_EMBEDDING", "true")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)

    with pytest.raises(ValueError, match="remote embedding"):
        RuntimeConfig.from_env()
