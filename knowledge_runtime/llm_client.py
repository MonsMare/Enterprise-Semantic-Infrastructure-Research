from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import KRProviderUnavailable


ALLOWED_MODELS = {"deepseek-v4.1-flash", "qwen3.8-max"}


@dataclass(frozen=True)
class ModelTurn:
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ModelClient(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn | dict[str, Any]: ...


class OpenAICompatibleClient:
    """Small chat-completions client; credentials are read from the environment."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 90.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1")).rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL", "qwen3.8-max")
        self.timeout = timeout
        if self.model not in ALLOWED_MODELS:
            raise ValueError(f"model must be one of: {', '.join(sorted(ALLOWED_MODELS))}")

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        if not self.api_key:
            raise KRProviderUnavailable("LLM_API_KEY is not set")
        # An empty tool list is an explicit end-of-retrieval turn.  Keeping
        # ``auto`` here lets some compatible servers replay a tool call from
        # the conversation even though no tools are offered, which can make
        # the Agent loop consume its whole iteration budget after it already
        # has sufficient evidence.  OpenAI-compatible endpoints support the
        # standard ``none`` choice for this case.
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto" if tools else "none",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise KRProviderUnavailable(f"LLM request failed with HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise KRProviderUnavailable(f"LLM request failed: {exc.__class__.__name__}") from exc
        except (ValueError, KeyError, IndexError) as exc:
            raise KRProviderUnavailable("LLM returned an invalid response") from exc
        choice = payload.get("choices", [{}])[0]
        message = choice.get("message", {})
        return ModelTurn(content=message.get("content"), tool_calls=message.get("tool_calls"))
