from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .models import Evidence, Locator, Page, ProviderDescriptor, ReadOptions, ResourceEntry, SearchHit, SearchOptions


class KnowledgeProvider(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    def list(self, *, scope: str | None = None, cursor: str | None = None, limit: int = 20) -> Page[ResourceEntry]: ...

    def find(self, pattern: str, *, scope: str | None = None, cursor: str | None = None, limit: int = 20) -> Page[ResourceEntry]: ...

    def search(self, query: str, *, scope: str | None = None, options: SearchOptions | None = None) -> Page[SearchHit]: ...

    def read(self, locator: Locator, options: ReadOptions | None = None) -> Evidence: ...

    def stat(self, locator: Locator) -> dict: ...
