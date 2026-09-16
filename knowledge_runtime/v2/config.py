from __future__ import annotations

import os
from dataclasses import dataclass, replace


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RuntimeConfig:
    private_mode: bool = True
    allow_remote_parser: bool = False
    allow_remote_embedding: bool = False
    allow_remote_agent: bool = False
    embedding_model: str = "qwen3.7-text-embedding"
    agent_model: str = "qwen3.8-max"
    agent_api_key: str = ""
    embedding_api_key: str = ""
    agent_base_url: str = ""
    embedding_base_url: str = ""
    local_model_base_url: str = ""
    local_model_name: str = ""
    database_url: str = ""
    local_state_path: str = ".kr-v2-data/canonical.sqlite"
    artifact_endpoint: str = ""
    artifact_bucket: str = "kr-v2-artifacts"
    opensearch_url: str = ""
    opensearch_index_prefix: str = "kr-v2"

    def __post_init__(self) -> None:
        if self.embedding_model != "qwen3.7-text-embedding":
            raise ValueError("v2 embedding model must be qwen3.7-text-embedding")
        if self.agent_model != "qwen3.8-max":
            raise ValueError("v2 Agent model must be qwen3.8-max")
        if self.allow_remote_agent and (not self.agent_api_key or not self.agent_base_url):
            raise ValueError("remote Agent requires QWEN_LLM_API_KEY and QWEN_LLM_BASE_URL")
        if self.allow_remote_embedding and (not self.embedding_api_key or not self.embedding_base_url):
            raise ValueError("remote embedding requires DASHSCOPE_API_KEY and DASHSCOPE_BASE_URL")

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        return cls(
            private_mode=_bool_env("KR_PRIVATE_MODE", True),
            allow_remote_parser=_bool_env("KR_ALLOW_REMOTE_PARSER", False),
            allow_remote_embedding=_bool_env("KR_ALLOW_REMOTE_EMBEDDING", False),
            allow_remote_agent=_bool_env("KR_ALLOW_REMOTE_AGENT", False),
            embedding_model="qwen3.7-text-embedding",
            agent_model="qwen3.8-max",
            agent_api_key=os.environ.get("QWEN_LLM_API_KEY", ""),
            embedding_api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
            agent_base_url=os.environ.get("QWEN_LLM_BASE_URL", ""),
            embedding_base_url=os.environ.get("DASHSCOPE_BASE_URL", ""),
            local_model_base_url=os.environ.get("KR_LOCAL_MODEL_BASE_URL", ""),
            local_model_name=os.environ.get("KR_LOCAL_MODEL_NAME", ""),
            database_url=os.environ.get("KR_DATABASE_URL", ""),
            local_state_path=os.environ.get("KR_LOCAL_STATE_PATH", ".kr-v2-data/canonical.sqlite"),
            artifact_endpoint=os.environ.get("KR_ARTIFACT_ENDPOINT", ""),
            artifact_bucket=os.environ.get("KR_ARTIFACT_BUCKET", "kr-v2-artifacts"),
            opensearch_url=os.environ.get("KR_OPENSEARCH_URL", ""),
            opensearch_index_prefix=os.environ.get("KR_OPENSEARCH_INDEX_PREFIX", "kr-v2"),
        )

    @classmethod
    def test_private(cls, **overrides: object) -> "RuntimeConfig":
        base = cls(
            private_mode=True,
            allow_remote_parser=False,
            allow_remote_embedding=False,
            allow_remote_agent=False,
            agent_api_key="",
            embedding_api_key="",
        )
        return replace(base, **overrides)

    def require_remote_parser(self) -> None:
        if self.private_mode and not self.allow_remote_parser:
            raise RuntimeError("remote parser is disabled in private mode")

    def require_remote_embedding(self) -> None:
        if self.private_mode and not self.allow_remote_embedding:
            raise RuntimeError("remote embedding is disabled in private mode")

    def require_remote_agent(self) -> None:
        if self.private_mode and not self.allow_remote_agent:
            raise RuntimeError("remote Agent is disabled in private mode")
