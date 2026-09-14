from __future__ import annotations

from dataclasses import replace
from typing import Any

from .assets import KnowledgeAssetStore
from .memory_provider import MemoryProvider
from .models import Evidence


class AssetKnowledgeProvider(MemoryProvider):
    """Queryable view over persisted parsed assets, preserving source revisions."""

    def __init__(self, store: KnowledgeAssetStore) -> None:
        self.store = store
        self._assets = {asset.asset_id: asset for asset in store.list_assets()}
        super().__init__({asset_id: asset.markdown for asset_id, asset in self._assets.items()}, provider_id="knowledge-assets")
        for asset_id, asset in self._assets.items():
            resource = self._resources[asset_id]
            self._resources[asset_id] = replace(resource, name=asset.source_name, media_type="text/markdown")

    def _revision(self, resource: Any, resource_id: str | None = None) -> str:
        asset = self._assets.get(resource_id or "")
        return asset.source_hash if asset else super()._revision(resource)

    def read(self, locator, options=None) -> Evidence:
        evidence = super().read(locator, options)
        asset = self._assets.get(locator.resource_id)
        return replace(evidence, derived_from=asset.asset_id if asset else None)

    def stat(self, locator) -> dict[str, Any]:
        result = super().stat(locator)
        asset = self._assets.get(locator.resource_id)
        if asset:
            result.update({"source_hash": asset.source_hash, "parser": asset.parser_name, "parser_version": asset.parser_version})
        return result
