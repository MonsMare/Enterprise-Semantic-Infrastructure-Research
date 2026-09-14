from __future__ import annotations

import base64
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Generic, Mapping, Sequence, TypeVar

MAX_PAGE_SIZE = 200
MAX_READ_BYTES = 1_000_000


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(_canonical(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def content_hash(content: str | bytes) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class KnowledgeChunk:
    """A stable, revision-scoped slice of an ingested knowledge asset."""

    chunk_id: str
    asset_id: str
    revision_id: str
    source_name: str
    heading_path: tuple[str, ...]
    start_line: int
    end_line: int
    text: str

    def __post_init__(self) -> None:
        if not self.chunk_id or not self.asset_id or not self.revision_id or not self.source_name:
            raise ValueError("chunk identity and source name are required")
        if self.start_line <= 0 or self.end_line < self.start_line:
            raise ValueError("chunk line range is invalid")
        if not self.text.strip():
            raise ValueError("chunk text must not be empty")
        if isinstance(self.heading_path, str):
            object.__setattr__(self, "heading_path", (self.heading_path,))
        else:
            object.__setattr__(self, "heading_path", tuple(str(item) for item in self.heading_path))


@dataclass(frozen=True)
class Locator:
    version: int
    provider: str
    resource_id: str
    revision: str
    selector: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ValueError("unsupported locator version")
        if not self.provider or not self.resource_id or not self.revision:
            raise ValueError("provider, resource_id and revision are required")

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "provider": self.provider,
            "resource_id": self.resource_id,
            "revision": self.revision,
            "selector": _canonical(self.selector) if self.selector is not None else None,
        }

    def to_json(self) -> str:
        return _json_bytes(self.as_dict()).decode("utf-8")

    @classmethod
    def from_json(cls, value: str) -> "Locator":
        data = json.loads(value)
        return cls(
            version=int(data["version"]),
            provider=str(data["provider"]),
            resource_id=str(data["resource_id"]),
            revision=str(data["revision"]),
            selector=data.get("selector"),
        )


@dataclass(frozen=True)
class SearchOptions:
    limit: int = 20
    cursor: str | None = None
    case_sensitive: bool = False
    semantic_weight: float = 0.35
    rrf_k: int = 60

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("limit must be positive")
        if self.limit > MAX_PAGE_SIZE:
            from .errors import KRLimitExceeded

            raise KRLimitExceeded(f"limit cannot exceed {MAX_PAGE_SIZE}")
        if (
            isinstance(self.semantic_weight, bool)
            or not isinstance(self.semantic_weight, (int, float))
            or not math.isfinite(self.semantic_weight)
            or not 0.0 <= self.semantic_weight <= 1.0
        ):
            raise ValueError("semantic_weight must be between 0 and 1")
        if isinstance(self.rrf_k, bool) or not isinstance(self.rrf_k, int) or self.rrf_k <= 0:
            raise ValueError("rrf_k must be a positive integer")


@dataclass(frozen=True)
class ReadOptions:
    max_bytes: int = 20_000
    representation: str = "text"

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if self.max_bytes > MAX_READ_BYTES:
            from .errors import KRLimitExceeded

            raise KRLimitExceeded(f"max_bytes cannot exceed {MAX_READ_BYTES}")


@dataclass(frozen=True)
class SearchHit:
    locator: Locator
    display_name: str
    preview: str | None = None
    match_spans: tuple[Mapping[str, Any], ...] = ()
    ordering_key: str = ""


@dataclass(frozen=True)
class ResourceEntry:
    locator: Locator
    name: str
    kind: str = "document"


T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: Sequence[T]
    next_cursor: str | None = None
    snapshot_id: str | None = None
    partial: bool = False


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    supported_operations: tuple[str, ...]
    query_language: str
    selector_types: tuple[str, ...]
    revision_model: str
    max_read_bytes: int


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    locator: Locator
    content: str | bytes
    content_hash: str
    media_type: str
    encoding: str
    source_revision: str
    resolved_selector: Mapping[str, Any] | None
    retrieved_at: str
    truncated: bool
    representation: str
    derived_from: str | None = None
    source_label: str | None = None

    @classmethod
    def from_content(
        cls,
        *,
        locator: Locator,
        content: str | bytes,
        media_type: str,
        representation: str,
        resolved_selector: Mapping[str, Any] | None,
        derived_from: str | None = None,
        source_label: str | None = None,
        truncated: bool = False,
        evidence_id: str | None = None,
    ) -> "Evidence":
        digest = content_hash(content)
        identity = hashlib.sha256(f"{locator.to_json()}\n{digest}".encode("utf-8")).hexdigest()[:16]
        return cls(
            evidence_id=evidence_id or f"ev-{identity}",
            locator=locator,
            content=content,
            content_hash=digest,
            media_type=media_type,
            encoding="utf-8" if isinstance(content, str) else "base64",
            source_revision=locator.revision,
            resolved_selector=resolved_selector,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            truncated=truncated,
            representation=representation,
            derived_from=derived_from,
            source_label=source_label,
        )

    def as_model_input(self) -> dict[str, Any]:
        content = self.content
        if isinstance(content, bytes):
            content = base64.b64encode(content).decode("ascii")
        return {
            "evidence_id": self.evidence_id,
            "locator": self.locator.to_json(),
            "source_revision": self.source_revision,
            "selector": self.resolved_selector,
            "content": content,
            "content_hash": self.content_hash,
            "media_type": self.media_type,
            "representation": self.representation,
            "truncated": self.truncated,
            "derived_from": self.derived_from,
            "source_label": self.source_label,
        }
