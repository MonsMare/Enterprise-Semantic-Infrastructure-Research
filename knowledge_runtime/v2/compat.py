from __future__ import annotations

from typing import Any

from ..models import Evidence, Locator, Page, ProviderDescriptor, ReadOptions, ResourceEntry, SearchHit, SearchOptions
from .access import KnowledgeAccessRuntime
from .context import ContextRuntime


class LegacyProviderAdapter:
    """Compatibility façade over the first-class L3 access runtime."""

    provider_id = KnowledgeAccessRuntime.provider_id

    def __init__(self, runtime: ContextRuntime | KnowledgeAccessRuntime) -> None:
        self.access = runtime if isinstance(runtime, KnowledgeAccessRuntime) else KnowledgeAccessRuntime(runtime)

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self.access.descriptor

    def list(self, *, scope: str | None = None, cursor: str | None = None, limit: int = 20) -> Page[ResourceEntry]:
        return self.access.list(scope=scope, cursor=cursor, limit=limit)

    def find(self, pattern: str, *, scope: str | None = None, cursor: str | None = None, limit: int = 20) -> Page[ResourceEntry]:
        return self.access.find(pattern, scope=scope, cursor=cursor, limit=limit)

    def search(self, query: str, *, scope: str | None = None, options: SearchOptions | None = None) -> Page[SearchHit]:
        return self.access.search(query, scope=scope, options=options)

    def read(self, locator: Locator, options: ReadOptions | None = None) -> Evidence:
        return self.access.read(locator, options)

    def stat(self, locator: Locator) -> dict[str, Any]:
        return self.access.stat(locator)
